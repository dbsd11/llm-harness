# Task runner - constructs an ExecutionAgent per task and reports events/result.
import threading
from concurrent.futures import ThreadPoolExecutor

from core.agents.execution_agent import ExecutionAgent
from core import ws_protocol as P
from logger import logger


class TaskRunner:
    """Runs tasks on a bounded pool (max_quota) and reports telemetry over WS."""

    def __init__(self, ws_client, max_quota: int):
        self.ws_client = ws_client
        self.max_quota = max_quota
        self.executor = ThreadPoolExecutor(
            max_workers=max_quota, thread_name_prefix="exec-task"
        )
        self._running = 0
        self._lock = threading.Lock()

    @property
    def running_count(self) -> int:
        with self._lock:
            return self._running

    def submit(self, task_id: str, parent_task_id: str, goal: str,
               context: dict) -> None:
        self.executor.submit(self._run, task_id, parent_task_id, goal, context)

    def _run(self, task_id: str, parent_task_id: str, goal: str,
             context: dict) -> None:
        with self._lock:
            self._running += 1

        role = context.get("role", "通用助手")
        system_prompt = context.get("system_prompt", "你是一个有帮助的智能助手。")

        agent = ExecutionAgent()
        try:
            # event: execution agent created
            self.ws_client.send(P.task_event_frame(
                task_id, P.EVENT_AGENT_CREATED, role=role))

            agent.initialize({"role": role, "system_prompt": system_prompt})

            # event: task started
            self.ws_client.send(P.task_event_frame(task_id, P.EVENT_TASK_STARTED))

            ctx = dict(context)
            ctx["goal"] = context.get("question", goal)
            ctx["task_id"] = task_id

            result = agent.run(task_id, ctx)

            self.ws_client.send(P.task_result_frame(
                task_id, result.get("success", False), result))
        except Exception as e:
            logger.error(f"Exec task {task_id} error: {e}")
            self.ws_client.send(P.task_result_frame(
                task_id, False, {"success": False, "output": "", "error": str(e)}))
        finally:
            agent.cleanup()
            with self._lock:
                self._running -= 1
