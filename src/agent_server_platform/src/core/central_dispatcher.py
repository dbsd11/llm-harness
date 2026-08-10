# CentralDispatcher - replaces the per-scenario in-process ExecutionWorker.
#
# One global consumer reads dispatch rows from the `messages` table and routes
# each to either a connected execution-agent-server (via WSDispatcher) or to
# local in-process execution. Reply rows are written in the same shape as the
# old ExecutionWorker, so SchedulingAgent.collect_replies is unchanged.
#
# Delivery semantics: ack-after-execute. A dispatch row is acked (acked=1) only
# after the task reaches a terminal state, so a crash mid-execution leaves it
# unacked and it is redelivered on the next consumer poll (or after a restart).
# An in-flight guard prevents the same task_id from being dispatched twice
# within one process; idempotency checks skip already-terminal tasks on
# redelivery. Startup orphan recovery (scenarios.scenario_manager) marks
# in-flight tasks/scenarios failed on restart so nothing stays "running" forever.
import os
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional, Set

from logger import logger
from core.message_queue import TaskMessage
from database.repositories.message_repository import MessageRepository
from database.repositories.task_repository import TaskRepository
from database.models.message import Message
from core.state_machine import TASK_EXECUTION_COMPLETE_STATES


# Execution-complete task states - a redelivered task in one of these is a no-op
# (do not re-execute). PENDING_REVIEW is included: it has run, it is just gated
# for human acceptance. See core.state_machine.TASK_EXECUTION_COMPLETE_STATES.


def _scenario_requires_acceptance(scenario_id: Optional[str]) -> bool:
    """Check if scenario requires manual acceptance (manual_acceptance flag in config)."""
    if not scenario_id:
        return False
    try:
        from database.repositories.scenario_repository import ScenarioRepository
        scenario = ScenarioRepository().find_by_scenario_id(scenario_id)
        if not scenario or not scenario.config:
            return False
        config = json.loads(scenario.config)
        return config.get("manual_acceptance", False)
    except Exception as e:
        logger.error(f"[Dispatcher] Failed to check manual_acceptance for {scenario_id}: {e}")
        return False


def finalize_task(scenario_id: Optional[str], task_id: str, result: dict,
                  agent_name: str = None, agent_role: str = None,
                  execution_duration: float = None) -> None:
    """Mark task state + write the reply Message row.

    Shared by both the local and the WS-forward execution paths so the reply
    wire format stays identical to the legacy ExecutionWorker. Stamps the
    execution-agent identity + duration onto the task row (the scheduling
    path does this in agent_manager; the execution path does it here).
    """
    task_repo = TaskRepository()
    msg_repo = MessageRepository()
    manual = _scenario_requires_acceptance(scenario_id)
    try:
        if manual:
            # Manual acceptance: route to PENDING_REVIEW for human review
            task_repo.mark_as_pending_review(
                task_id, json.dumps(result, ensure_ascii=False),
                agent_name=agent_name, agent_role=agent_role,
                execution_duration=execution_duration,
            )
        elif result.get("success"):
            task_repo.mark_as_completed(
                task_id, json.dumps(result, ensure_ascii=False),
                agent_name=agent_name, agent_role=agent_role,
                execution_duration=execution_duration,
            )
        else:
            task_repo.mark_as_failed(task_id, result.get("error", "Unknown"))
    except Exception as e:
        logger.error(f"[Dispatcher] Failed to update task state for {task_id}: {e}")

    try:
        msg_repo.create(Message(
            scenario_id=scenario_id,
            task_id=task_id,
            from_agent="execution",
            to_agent="scheduling",
            message_type="reply",
            content=json.dumps({
                "task_id": task_id,
                "success": result.get("success", False),
                "pending_review": manual,
                "result": result,
            }, ensure_ascii=False),
            timestamp=datetime.now(),
        ))
    except Exception as pe:
        logger.error(f"[Dispatcher] Failed to persist reply message: {pe}")


def run_task_locally(scenario_id: Optional[str], msg: TaskMessage) -> None:
    """Execute a single task in-process (the legacy path).

    Body extracted verbatim from the deleted ExecutionWorker._execute_task.
    Used both for the no-server fallback and as a safety net.
    """
    from core.agents.execution_agent import ExecutionAgent

    task_repo = TaskRepository()

    # Idempotency: if a previous run already drove this task to a terminal
    # state (e.g. redelivery after a crash), don't re-execute.
    existing = task_repo.find_by_task_id(msg.task_id)
    if existing and existing.state in TASK_EXECUTION_COMPLETE_STATES:
        logger.info(f"[Local] Task {msg.task_id} already terminal "
                    f"({existing.state}); skipping redelivery")
        return

    start_time = time.time()

    agent = ExecutionAgent()
    agent_config = {
        "role": msg.context.get("role", "通用助手"),
        "system_prompt": msg.context.get("system_prompt", "你是一个有帮助的智能助手。"),
    }
    agent.initialize(agent_config)

    try:
        task_repo.mark_as_started(msg.task_id)

        ctx = msg.context.copy()
        ctx["goal"] = msg.context.get("question", msg.goal)
        ctx["task_id"] = msg.task_id

        logger.info(f"[Local] Starting task {msg.task_id} role={agent_config['role']}")
        result = agent.run(msg.task_id, ctx)
        elapsed = time.time() - start_time
        logger.info(f"[Local] Task {msg.task_id} done in {elapsed:.2f}s "
                    f"success={result.get('success')}")

        finalize_task(scenario_id, msg.task_id, result,
                      agent_name="ExecutionAgent",
                      agent_role=agent_config["role"],
                      execution_duration=round(elapsed, 3))

    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(f"[Local] Task {msg.task_id} error after {elapsed:.2f}s: {e}")
        finalize_task(scenario_id, msg.task_id, {
            "success": False, "output": "", "error": str(e),
        })
    finally:
        agent.cleanup()


class CentralDispatcher:
    """Global consumer of dispatch rows; routes to WS exec-servers or local."""

    def __init__(self, max_workers: int = None):
        self.max_workers = max_workers or int(os.getenv("LOCAL_EXEC_MAX_WORKERS", "3"))
        self._stop = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.executor: Optional[ThreadPoolExecutor] = None
        self.ws = None  # WSDispatcher reference; set via set_ws_dispatcher()
        # task_ids currently executing in this process — prevents the same
        # unacked dispatch from being submitted twice before it completes.
        self._in_flight: Set[str] = set()
        self._in_flight_lock = threading.Lock()

    def set_ws_dispatcher(self, ws) -> None:
        self.ws = ws

    def start(self) -> None:
        self.executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="central-exec",
        )
        self.thread = threading.Thread(
            target=self._run, daemon=True, name="central-dispatcher"
        )
        self.thread.start()
        logger.info(f"CentralDispatcher started (local workers={self.max_workers})")

    def _ack(self, dispatch_id: Optional[int], task_id: str) -> None:
        """Ack a dispatch message + release the in-flight slot.

        Called by the local path after run_task_locally returns, and by the WS
        path (forward_task) after finalize. Safe to call with dispatch_id=None
        (legacy/defensive).
        """
        if dispatch_id is not None:
            try:
                MessageRepository().ack_message(dispatch_id)
            except Exception as e:
                logger.error(f"[Dispatcher] Failed to ack message {dispatch_id}: {e}")
        with self._in_flight_lock:
            self._in_flight.discard(task_id)

    def _run(self) -> None:
        msg_repo = MessageRepository()

        while not self._stop.is_set():
            try:
                pending = msg_repo.find_unacked_dispatch(limit=10)
            except Exception as e:
                logger.error(f"[Dispatcher] find_unacked_dispatch failed: {e}")
                time.sleep(1)
                continue

            if not pending:
                time.sleep(0.5)
                continue

            for rec in pending:
                try:
                    content = json.loads(rec.content) if rec.content else {}
                    task_msg = TaskMessage(
                        task_id=content.get("task_id", rec.task_id),
                        parent_task_id=content.get("parent_task_id", ""),
                        goal=content.get("goal", ""),
                        context=content.get("context", {}),
                    )
                except (json.JSONDecodeError, TypeError):
                    logger.error(f"Failed to parse dispatch message {rec.id}")
                    self._ack(rec.id, f"__bad_{rec.id}")
                    continue

                task_id = task_msg.task_id
                # In-flight guard: same task already executing here -> skip
                # (its dispatch is still unacked; will be acked on completion).
                with self._in_flight_lock:
                    if task_id in self._in_flight:
                        continue
                    self._in_flight.add(task_id)

                self.executor.submit(self._dispatch_one, rec.scenario_id, task_msg, rec.id)

        if self.executor:
            self.executor.shutdown(wait=True)
        logger.info("CentralDispatcher stopped")

    def _dispatch_one(self, scenario_id: Optional[str], msg: TaskMessage,
                      dispatch_id: Optional[int]) -> None:
        """Route one task: WS forward (if server selected + connected),
        defer (server selected but offline), or local execution (no server).

        ack happens on completion (local: after run_task_locally returns;
        WS: in forward_task after finalize). defer does NOT ack.
        """
        server_id = msg.context.get("server_id")

        if not server_id:
            # No server selected -> current behavior: execute locally.
            try:
                run_task_locally(scenario_id, msg)
            finally:
                self._ack(dispatch_id, msg.task_id)
            return

        if not self.ws:
            # WS subsystem not running -> safety fallback to local.
            logger.warning(f"[Dispatcher] No WS subsystem; task {msg.task_id} "
                           f"with server_id={server_id} runs locally")
            try:
                run_task_locally(scenario_id, msg)
            finally:
                self._ack(dispatch_id, msg.task_id)
            return

        if self.ws.is_connected(server_id):
            # Fire-and-forget on the WS loop; forward_task owns the lifecycle
            # (mark started -> send -> await -> finalize -> ack, or defer on drop).
            self.ws.schedule_forward(server_id, scenario_id, msg, dispatch_id)
        else:
            # Server offline -> park until it reconnects. Stays unacked +
            # in-flight so the consumer won't redeliver within this process;
            # on reconnect _drain_deferred re-forwards, eventually acking.
            self.ws.defer(server_id, scenario_id, msg, dispatch_id)

    def stop(self) -> None:
        self._stop.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=10)
        if self.executor:
            self.executor.shutdown(wait=False)


# Global singleton (started by the WS-server process)
central_dispatcher = CentralDispatcher()


# ── self-check ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # ponytail: one runnable check — in-flight guard + ack semantics, no DB.
    import core.central_dispatcher as _self

    acked_ids = []

    class _StubMsgRepo:
        def find_unacked_dispatch(self, limit=100):
            return []
        def ack_message(self, mid):
            acked_ids.append(mid)

    _real_repo = MessageRepository
    globals()["MessageRepository"] = _StubMsgRepo
    d = CentralDispatcher()
    try:
        # _ack acks the message + releases the in-flight slot
        with d._in_flight_lock:
            d._in_flight.add("t1")
        d._ack(7, "t1")
        assert acked_ids == [7], f"ack should record id 7: {acked_ids}"
        assert "t1" not in d._in_flight, "ack should release in-flight slot"
        # dispatch_id=None still releases the slot, no ack
        with d._in_flight_lock:
            d._in_flight.add("t2")
        d._ack(None, "t2")
        assert acked_ids == [7] and "t2" not in d._in_flight, "None id: no ack, slot released"

        # idempotency: terminal task -> run_task_locally skips execution
        from core.message_queue import TaskMessage
        calls = {"run": 0}
        class _FakeAgent:
            def initialize(self, cfg): pass
            def run(self, *a, **k): calls["run"] += 1; return {"success": True, "output": "x"}
            def cleanup(self): pass
        class _StubTaskRepo:
            def __init__(self): self.state = "success"  # already terminal
            def find_by_task_id(self, tid):
                class T: state = self.state
                return T()
            def mark_as_started(self, tid): pass
            def mark_as_completed(self, *a, **k): pass
            def mark_as_failed(self, *a, **k): pass
        _self_TaskRepository = TaskRepository
        globals()["TaskRepository"] = _StubTaskRepo
        # patch ExecutionAgent import inside run_task_locally
        import core.agents.execution_agent as _ea
        _orig_exec_cls = _ea.ExecutionAgent
        _ea.ExecutionAgent = _FakeAgent
        try:
            run_task_locally("sc", TaskMessage(task_id="t9", parent_task_id="",
                                               goal="g", context={}))
            assert calls["run"] == 0, "terminal task must not re-execute"
        finally:
            _ea.ExecutionAgent = _orig_exec_cls
            globals()["TaskRepository"] = _self_TaskRepository
    finally:
        globals()["MessageRepository"] = _real_repo

    print("central_dispatcher self-check OK")
