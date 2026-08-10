# Scenario Manager - scenario lifecycle management
import uuid
import json
import threading
from typing import Dict, Any, Optional, List
from datetime import datetime

from database.repositories.scenario_repository import ScenarioRepository
from database.models.scenario import Scenario
from core.state_machine import ScenarioState, SCENARIO_STATE_MACHINE, TASK_TERMINAL_STATES
from core.event_bus import event_bus
from scenarios.base_scenario import BaseScenario, ScenarioContext
from logger import logger


class ScenarioManager:
    """
    Scenario Manager: Lifecycle management and orchestration.

    Responsibilities:
    - Scenario registration and management
    - Scenario lifecycle (start/stop/cleanup)
    - State machine transitions
    - Agent orchestration
    """

    def __init__(self):
        self.scenario_repo = ScenarioRepository()
        self.active_scenarios: Dict[str, BaseScenario] = {}
        self.scenario_contexts: Dict[str, ScenarioContext] = {}
        self.lock = threading.Lock()

    def create_scenario(self, scenario_type: str, name: str, description: str = "",
                       config: Dict[str, Any] = None, created_by: int = None) -> str:
        """
        Create a new scenario.

        Args:
            scenario_type: Scenario type (simple_qa, code_execution)
            name: Scenario name
            description: Scenario description
            config: Scenario configuration
            created_by: User ID who created the scenario

        Returns:
            Scenario ID
        """
        scenario_id = str(uuid.uuid4())
        trace_id = str(uuid.uuid4())  # Trace for this scenario's lifecycle

        scenario = Scenario(
            scenario_id=scenario_id,
            scenario_type=scenario_type,
            name=name,
            description=description,
            state=ScenarioState.INITIALIZING.value,
            config=json.dumps(config or {}),
            context=json.dumps({"trace_id": trace_id}),
            created_by=created_by,
            created_at=datetime.now(),
            updated_at=datetime.now()
        )

        self.scenario_repo.create(scenario)

        event_bus.emit("scenario.created", {
            "scenario_id": scenario_id,
            "scenario_type": scenario_type,
            "name": name,
        }, trace_id=trace_id)

        logger.info(f"Created scenario: {scenario_id} ({scenario_type}) trace:{trace_id}")
        return scenario_id

    def update_scenario(self, scenario_id: str, name: str = None,
                        description: str = None, config: Dict[str, Any] = None,
                        scenario_type: str = None) -> bool:
        """更新已有场景的可编辑字段（scenario_type/name/description/config），不改状态。

        用于「确认保存」对已落库场景的二次修改：避免每次都新建一条记录。
        scenario_type 一并更新，否则修改类型（如 legacy debate → simple_qa）时类型字段
        不变，造成「保存了但查看没变化」+ 类型/配置不一致无法启动。
        """
        scenario = self.scenario_repo.find_by_scenario_id(scenario_id)
        if not scenario:
            logger.error(f"update_scenario: scenario not found: {scenario_id}")
            return False

        # Enforce editability rule (D6): manual_acceptance editable only while initializing
        if config is not None and scenario.state != ScenarioState.INITIALIZING.value:
            if "manual_acceptance" in config:
                logger.warning(f"update_scenario: manual_acceptance cannot be changed "
                               f"after scenario leaves initializing state")
                # Strip manual_acceptance from config, proceed with other fields
                config = {k: v for k, v in config.items() if k != "manual_acceptance"}

        if scenario_type is not None:
            scenario.scenario_type = scenario_type
        if name is not None:
            scenario.name = name
        if description is not None:
            scenario.description = description
        if config is not None:
            scenario.config = json.dumps(config)
        scenario.updated_at = datetime.now()

        ok = self.scenario_repo.update(scenario)

        # 传播 trace_id 用于事件追踪
        trace_id = ""
        try:
            trace_id = json.loads(scenario.context or "{}").get("trace_id", "") or ""
        except (json.JSONDecodeError, TypeError):
            pass
        event_bus.emit("scenario.updated", {
            "scenario_id": scenario_id,
            "name": scenario.name,
        }, trace_id=trace_id)

        logger.info(f"Updated scenario: {scenario_id}")
        return ok

    def start_scenario(self, scenario_id: str, scenario_instance: BaseScenario) -> bool:
        """
        Start scenario execution.

        Args:
            scenario_id: Scenario ID
            scenario_instance: Scenario instance to execute

        Returns:
            True if started successfully
        """
        # Get scenario from database
        scenario = self.scenario_repo.find_by_scenario_id(scenario_id)
        if not scenario:
            logger.error(f"Scenario not found: {scenario_id}")
            return False

        # Validate state transition
        sm = SCENARIO_STATE_MACHINE
        sm.initialize(ScenarioState(scenario.state))

        if not sm.can_transition(ScenarioState.RUNNING):
            logger.error(f"Invalid state transition: {scenario.state} -> running")
            return False

        # Update state
        self.scenario_repo.update_scenario_state(scenario_id, ScenarioState.RUNNING.value)

        # Register scenario
        with self.lock:
            self.active_scenarios[scenario_id] = scenario_instance

        # Extract trace_id from scenario context (propagate through lifecycle)
        scenario_context = json.loads(scenario.context) if scenario.context else {}
        trace_id = scenario_context.get("trace_id")

        # Start execution in background thread
        config = json.loads(scenario.config) if scenario.config else {}
        config["scenario_id"] = scenario_id
        config["trace_id"] = trace_id

        # Materialize the scenario's declared agent topology (scheduling +
        # execution roles) into the agents table. In the decoupled architecture
        # execution agents run on remote exec-servers and are no longer
        # registered as a side-effect of task submission — register the declared
        # roles here so the registry reflects who participates. Scoped to the
        # scenario lifecycle (cleaned by _release_scenario_agents on release).
        self._register_declared_agents(scenario_id, config)

        thread = threading.Thread(
            target=self._execute_scenario,
            args=(scenario_id, scenario_instance, config, trace_id),
            daemon=True
        )
        thread.start()

        event_bus.emit("scenario.started", {
            "scenario_id": scenario_id,
        }, trace_id=trace_id)

        logger.info(f"Started scenario: {scenario_id} trace:{trace_id}")
        return True

    def _register_declared_agents(self, scenario_id: str, config: Dict[str, Any]) -> None:
        """Register the scenario's declared agent topology into the agents table.

        Writes one row per declared agent (scheduling + each execution role),
        with the role stored in `config` so the registry can render it. Rows are
        scoped to the scenario and removed by _release_scenario_agents on
        release. Best-effort: failures log but do not block scenario start.
        """
        from database.repositories.agent_repository import AgentRepository
        from database.models.agent import Agent

        roles = (config or {}).get("agent_roles") or {}
        repo = AgentRepository()
        now = datetime.now()

        def _create(agent_type, name, cfg):
            try:
                repo.create(Agent(
                    agent_id=str(uuid.uuid4()),
                    scenario_id=scenario_id,
                    agent_type=agent_type,
                    name=name,
                    description="",
                    config=json.dumps(cfg, ensure_ascii=False),
                    status="active",
                    created_at=now, updated_at=now,
                ))
            except Exception as e:
                logger.error(f"Failed to register {agent_type} agent for "
                             f"scenario {scenario_id}: {e}")

        # Scheduling agent
        sched = roles.get("scheduling_agent") or {}
        _create("scheduling", "调度Agent",
                {"role": sched.get("role") or "任务调度专家"})

        # Execution agents (declared roles)
        for ag in roles.get("execution_agents") or []:
            if not isinstance(ag, dict):
                continue
            name = ag.get("name") or ag.get("role") or "执行Agent"
            cfg = {"role": ag.get("role") or ""}
            if ag.get("server_id"):
                cfg["server_id"] = ag["server_id"]
            _create("execution", name, cfg)

    def _execute_scenario(self, scenario_id: str, scenario: BaseScenario,
                         config: Dict[str, Any], trace_id: str = None):
        """Execute scenario in background thread with trace propagation"""
        try:
            # Execute scenario
            result = scenario.start(config)

            if config.get("manual_acceptance"):
                # Manual acceptance: cycle 1 paused or completed-with-no-tasks.
                # The coordinator owns state from here: do NOT auto-complete, do NOT release agents.
                logger.info(f"Scenario {scenario_id} manual-acceptance cycle 1 yielded")
                return

            # run() returns {success: bool, ...}; wait_for_task surfaces a timeout
            # as state="timeout" without raising, so success=False. Route to FAILED
            # so a timeout/task-failure doesn't masquerade as COMPLETED.
            if isinstance(result, dict) and result.get("success") is False:
                error_msg = (result.get("error")
                             or result.get("task_state")
                             or "scenario did not succeed (task timeout or failure)")
                self.scenario_repo.mark_as_failed(scenario_id)
                event_bus.emit("scenario.failed", {
                    "scenario_id": scenario_id,
                    "error": error_msg,
                    "result": result,
                }, trace_id=trace_id)
                logger.warning(f"Scenario failed (non-success result): "
                               f"{scenario_id} - {error_msg}")
            else:
                self.scenario_repo.mark_as_completed(scenario_id)
                event_bus.emit("scenario.completed", {
                    "scenario_id": scenario_id,
                    "result": result,
                }, trace_id=trace_id)
                logger.info(f"Scenario completed: {scenario_id}")

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Scenario execution error: {error_msg}")

            # Update state to failed
            self.scenario_repo.mark_as_failed(scenario_id)

            event_bus.emit("scenario.failed", {
                "scenario_id": scenario_id,
                "error": error_msg,
            }, trace_id=trace_id)

        finally:
            if config.get("manual_acceptance"):
                # Keep agents registered for subsequent cycles; release only on COMPLETED/FAILED.
                with self.lock:
                    self.active_scenarios.pop(scenario_id, None)
            else:
                # Release agent runtime resources (释放底层agent运行环境)
                self._release_scenario_agents(scenario_id, trace_id)

                # Cleanup
                with self.lock:
                    if scenario_id in self.active_scenarios:
                        del self.active_scenarios[scenario_id]

    def _release_scenario_agents(self, scenario_id: str, trace_id: str = None):
        """
        Release all resources for a scenario (释放底层agent运行环境).

        1. Stop execution worker + clean consumer offsets
        2. Cancel non-terminal tasks
        3. Cleanup agent runs (call agent.cleanup + remove from registry)
        """
        from core.agents.agent_manager import agent_manager
        from database.repositories.task_repository import TaskRepository
        from database.repositories.consumer_offset_repository import ConsumerOffsetRepository

        # 1. Clean consumer offsets for this scenario
        try:
            offset_repo = ConsumerOffsetRepository()
            for prefix in ("execution_worker", "scheduling_agent"):
                consumer_id = f"{prefix}:{scenario_id}"
                offset = offset_repo.find_by_id(consumer_id)
                if offset:
                    offset_repo.delete(consumer_id)
        except Exception as e:
            logger.error(f"Failed to clean consumer offsets for {scenario_id}: {e}")

        # 2b. Clean agent DB registrations for this scenario
        try:
            from database.repositories.agent_repository import AgentRepository
            agent_repo = AgentRepository()
            deleted = agent_repo.delete_by_scenario_id(scenario_id)
            if deleted > 0:
                logger.info(f"Cleaned {deleted} agent DB record(s) for scenario {scenario_id}")
        except Exception as e:
            logger.error(f"Failed to clean agent DB records for {scenario_id}: {e}")

        # 3. Process all tasks for this scenario
        task_repo = TaskRepository()
        tasks = task_repo.find_by_scenario_id(scenario_id)
        terminal_states = TASK_TERMINAL_STATES
        released_runs = 0
        cancelled_tasks = 0

        for task in tasks:
            # Cancel non-terminal tasks
            if task.state not in terminal_states:
                task_repo.mark_as_failed(task.task_id, "Cancelled: scenario ended")
                cancelled_tasks += 1

            # Cleanup agent run (call agent.cleanup + remove from registry)
            if task.agent_run_id:
                with agent_manager.lock:
                    agent_run = agent_manager.agent_runs.pop(task.agent_run_id, None)
                if agent_run:
                    try:
                        agent_run.agent.cleanup()
                    except Exception as e:
                        logger.error(f"Agent cleanup error for run {task.agent_run_id}: {e}")
                released_runs += 1

        if released_runs > 0 or cancelled_tasks > 0:
            event_bus.emit("scenario.agents_released", {
                "scenario_id": scenario_id,
                "released_runs": released_runs,
                "cancelled_tasks": cancelled_tasks,
            }, trace_id=trace_id)
            logger.info(f"Scenario {scenario_id}: released {released_runs} agent run(s), "
                       f"cancelled {cancelled_tasks} task(s)")

    def review_task(self, task_id: str, passed: bool, feedback: str = None) -> bool:
        """Human acceptance entry point (called from UI + REST).

        Args:
            task_id: Task ID to review
            passed: True for pass, False for fail
            feedback: Optional feedback comment (required for fail)

        Returns:
            True if review succeeded, False otherwise
        """
        from database.repositories.task_repository import TaskRepository
        task_repo = TaskRepository()

        # Apply the review decision
        ok = task_repo.mark_as_reviewed(task_id, passed, feedback)
        if not ok:
            logger.warning(f"review_task: mark_as_reviewed failed for {task_id}")
            return False

        # Get the task to find its scenario
        task = task_repo.find_by_task_id(task_id)
        if not task or not task.scenario_id:
            logger.warning(f"review_task: task {task_id} has no scenario_id")
            return False

        # Get scenario trace_id for event persistence
        scenario = self.scenario_repo.find_by_scenario_id(task.scenario_id)
        scenario_context = json.loads(scenario.context) if scenario and scenario.context else {}
        trace_id = scenario_context.get("trace_id")

        event_bus.emit("task.reviewed", {
            "task_id": task_id,
            "scenario_id": task.scenario_id,
            "passed": passed,
            "feedback": feedback,
        }, trace_id=trace_id)

        # Check if we can advance the review cycle
        self.advance_review_cycle(task.scenario_id)
        return True

    def advance_review_cycle(self, scenario_id: str) -> None:
        """Called when a review resolves the last PENDING_REVIEW task.

        Under self.lock; idempotent (skip if state != AWAITING_REVIEW).
        """
        from database.repositories.task_repository import TaskRepository
        from core.agents.agent_manager import agent_manager

        with self.lock:
            scenario = self.scenario_repo.find_by_scenario_id(scenario_id)
            if not scenario or scenario.state != ScenarioState.AWAITING_REVIEW.value:
                return

            # Extract trace_id early to ensure completion events are linked to the scenario trace
            scenario_context = json.loads(scenario.context) if scenario.context else {}
            trace_id = scenario_context.get("trace_id")

            task_repo = TaskRepository()

            # Check if any PENDING_REVIEW remain
            pending_review_count = task_repo.count_pending_review_by_scenario(scenario_id)
            if pending_review_count > 0:
                return  # Wait for more reviews

            # Load all tasks to check state
            all_tasks = task_repo.find_by_scenario_id(scenario_id)
            has_failed_with_feedback = any(
                t.state == "failed" and t.review_feedback for t in all_tasks)
            has_pending = any(t.state == "pending" for t in all_tasks)

            if has_failed_with_feedback or has_pending:
                # Submit resume scheduling task
                self.scenario_repo.update_scenario_state(
                    scenario_id, ScenarioState.RUNNING.value)

                # Load scenario config for context
                config = json.loads(scenario.config) if scenario.config else {}

                # Submit resume scheduling task
                agent_manager.submit_task(
                    goal=config.get("question", config.get("goal", "Resume scenario")),
                    agent_type="scheduling",
                    scenario_id=scenario_id,
                    priority=config.get("priority", 0),
                    timeout_seconds=config.get("timeout", 300),
                    context={
                        **config,
                        "scenario_id": scenario_id,
                        "resume_cycle": True,
                        "manual_acceptance": True,
                        "agent_roles": config.get("agent_roles", {}),
                    },
                )

                event_bus.emit("scenario.review_cycle_advanced", {
                    "scenario_id": scenario_id,
                    "reason": "resume_cycle",
                }, trace_id=trace_id)

                logger.info(f"Scenario {scenario_id} advanced to next review cycle")
            else:
                # All tasks passed, no pending -> COMPLETED
                self.scenario_repo.mark_as_completed(scenario_id)
                self._release_scenario_agents(scenario_id, trace_id=trace_id)

                event_bus.emit("scenario.completed", {
                    "scenario_id": scenario_id,
                    "reason": "all_tasks_passed",
                }, trace_id=trace_id)

                logger.info(f"Scenario {scenario_id} completed (all tasks passed review)")

    def on_cycle_paused(self, scenario_id: str) -> None:
        """Called by agent_manager when a scheduling task returns paused_for_review."""
        with self.lock:
            scenario = self.scenario_repo.find_by_scenario_id(scenario_id)
            if not scenario:
                return

            # Only transition if currently RUNNING
            if scenario.state != ScenarioState.RUNNING.value:
                return

            self.scenario_repo.update_scenario_state(
                scenario_id, ScenarioState.AWAITING_REVIEW.value)

            scenario_context = json.loads(scenario.context) if scenario.context else {}
            trace_id = scenario_context.get("trace_id")

            event_bus.emit("scenario.awaiting_review", {
                "scenario_id": scenario_id,
            }, trace_id=trace_id)

            logger.info(f"Scenario {scenario_id} paused for manual review")

    def stop_scenario(self, scenario_id: str) -> bool:
        """
        Stop scenario execution.

        Args:
            scenario_id: Scenario ID

        Returns:
            True if stopped successfully
        """
        scenario = self.scenario_repo.find_by_scenario_id(scenario_id)
        if not scenario:
            logger.error(f"Scenario not found: {scenario_id}")
            return False

        # Extract trace_id for event propagation
        scenario_context = json.loads(scenario.context) if scenario.context else {}
        trace_id = scenario_context.get("trace_id")

        # Update state to cancelled
        self.scenario_repo.update_scenario_state(scenario_id, ScenarioState.CANCELLED.value)

        # Remove from active scenarios
        with self.lock:
            if scenario_id in self.active_scenarios:
                # Cleanup scenario
                try:
                    self.active_scenarios[scenario_id].cleanup()
                except:
                    pass
                del self.active_scenarios[scenario_id]

        # Release all agent resources (worker, tasks, agent runs, consumer offsets)
        self._release_scenario_agents(scenario_id, trace_id)

        event_bus.emit("scenario.stopped", {
            "scenario_id": scenario_id,
        }, trace_id=trace_id)

        logger.info(f"Stopped scenario: {scenario_id}")
        return True

    def get_scenario_status(self, scenario_id: str) -> Optional[Dict[str, Any]]:
        """Get scenario status"""
        scenario = self.scenario_repo.find_by_scenario_id(scenario_id)
        if not scenario:
            return None

        return {
            "scenario_id": scenario.scenario_id,
            "scenario_type": scenario.scenario_type,
            "name": scenario.name,
            "state": scenario.state,
            "config": json.loads(scenario.config) if scenario.config else {},
            "created_at": scenario.created_at.isoformat() if scenario.created_at else None,
            "started_at": scenario.started_at.isoformat() if scenario.started_at else None,
            "completed_at": scenario.completed_at.isoformat() if scenario.completed_at else None,
        }

    def list_scenarios(self, limit: int = 100) -> List[Dict[str, Any]]:
        """List all scenarios"""
        scenarios = self.scenario_repo.find_all(limit=limit)
        return [
            {
                "scenario_id": s.scenario_id,
                "scenario_type": s.scenario_type,
                "name": s.name,
                "state": s.state,
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in scenarios
        ]

    def shutdown(self):
        """Shutdown scenario manager"""
        # Stop all active scenarios
        with self.lock:
            for scenario_id in list(self.active_scenarios.keys()):
                self.stop_scenario(scenario_id)

        logger.info("ScenarioManager shutdown")


# Global scenario manager instance
scenario_manager = ScenarioManager()


def recover_orphans_on_startup() -> None:
    """Mark in-flight scenarios/tasks orphaned by a platform restart as failed.

    Scenario execution threads live in the Flask process; task execution lives
    in the WS process. A platform restart kills both, so any scenario still
    `running` and any task still `pending`/`running`/`waiting` has no live
    runner and would otherwise stay "running" forever. This makes them visible
    failures instead. Full scenario resume (checkpointing) is future work.

    Idempotent: only touches non-terminal rows, so repeated restarts are safe.
    """
    from database.repositories.task_repository import TaskRepository

    srepo = ScenarioRepository()
    trepo = TaskRepository()

    n_scenarios = 0
    for s in srepo.find_by_state(ScenarioState.RUNNING.value):
        srepo.mark_as_failed(s.scenario_id)
        n_scenarios += 1

    n_tasks = 0
    for state in ("pending", "running", "waiting"):
        for t in trepo.find_by_state(state):
            trepo.mark_as_failed(t.task_id, "Orphaned by platform restart")
            n_tasks += 1

    if n_scenarios or n_tasks:
        logger.info(f"Startup orphan recovery: marked {n_scenarios} scenario(s) "
                    f"and {n_tasks} task(s) as failed")
        event_bus.emit("recovery.orphans_marked", {
            "scenarios": n_scenarios, "tasks": n_tasks,
        })
    else:
        logger.info("Startup orphan recovery: no orphans found")


# ── self-check ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # ponytail: one runnable check — declared topology materializes to rows
    import json as _json

    created = []

    class _StubRepo:
        def create(self, agent):
            created.append(agent)

    cfg = {
        "agent_roles": {
            "scheduling_agent": {"role": "调度专家"},
            "execution_agents": [
                {"name": "数学家", "role": "数学专家", "server_id": "srv-1"},
                {"name": "作家", "role": "写作专家"},
            ],
        }
    }
    sm = ScenarioManager()
    sm.scenario_repo = None  # not used by _register_declared_agents
    # inject stub repo into the method's import scope
    import scenarios.scenario_manager as _self
    _real_AgentRepository = _self.__dict__.get("AgentRepository")
    # _register_declared_agents imports AgentRepository lazily inside the func;
    # patch the module attribute used by the import.
    import database.repositories.agent_repository as _arepo_mod
    _orig = _arepo_mod.AgentRepository
    _arepo_mod.AgentRepository = _StubRepo
    try:
        sm._register_declared_agents("sc-123", cfg)
    finally:
        _arepo_mod.AgentRepository = _orig

    types = [a.agent_type for a in created]
    assert types == ["scheduling", "execution", "execution"], f"rows: {types}"
    sched_cfg = _json.loads(created[0].config)
    assert created[0].name == "调度Agent" and sched_cfg["role"] == "调度专家"
    exec1 = _json.loads(created[1].config)
    assert created[1].name == "数学家" and exec1["role"] == "数学专家" \
        and exec1["server_id"] == "srv-1"
    assert created[2].name == "作家"
    # empty agent_roles → still registers a scheduling agent with default role
    created.clear()
    _arepo_mod.AgentRepository = _StubRepo
    try:
        sm._register_declared_agents("sc-456", {})
    finally:
        _arepo_mod.AgentRepository = _orig
    assert len(created) == 1 and created[0].agent_type == "scheduling" \
        and _json.loads(created[0].config)["role"], "empty config fallback"

    # recover_orphans_on_startup: running scenarios + non-terminal tasks -> failed
    s_marked, t_marked = [], []

    class _SRepo:
        def find_by_state(self, state):
            if state == "running":
                class S: scenario_id = "sc-run"
                return [S()]
            return []
        def mark_as_failed(self, sid): s_marked.append(sid)
    class _TRepo:
        def find_by_state(self, state):
            if state == "running":
                class T: task_id = "t-run"
                return [T()]
            if state == "pending":
                class T: task_id = "t-pend"
                return [T()]
            return []
        def mark_as_failed(self, tid, err): t_marked.append(tid)

    _real_sr = ScenarioRepository
    _real_tr = globals().get("TaskRepository")
    globals()["ScenarioRepository"] = _SRepo
    import scenarios.scenario_manager as _smmod
    # recover_orphans_on_startup imports TaskRepository lazily inside the func
    import database.repositories.task_repository as _tmod
    _orig_tr = _tmod.TaskRepository
    _tmod.TaskRepository = _TRepo
    try:
        recover_orphans_on_startup()
    finally:
        _tmod.TaskRepository = _orig_tr
        globals()["ScenarioRepository"] = _real_sr
    assert s_marked == ["sc-run"], f"running scenario should be failed: {s_marked}"
    assert set(t_marked) == {"t-run", "t-pend"}, f"non-terminal tasks failed: {t_marked}"

    print("scenario_manager self-check OK")
