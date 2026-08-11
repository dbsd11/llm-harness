# Code Execution Scenario - execute code in sandbox
import json
import uuid
from datetime import datetime
from typing import Dict, Any
from scenarios.base_scenario import BaseScenario
from core.state_machine import TaskState
from core.message_queue import mqs, TaskMessage
from core.event_bus import event_bus
from database.models.task import Task
from database.repositories.task_repository import TaskRepository
from logger import logger


class CodeExecutionScenario(BaseScenario):
    """
    Code Execution Scenario: Execute code in sandbox.

    Workflow:
    1. Receive code/script from config
    2. Create task row and dispatch via mqs to remote execution server
    3. Execution server's ReAct agent runs code in sandbox
    4. Collect reply and return result
    """

    def get_scenario_type(self) -> str:
        return "code_execution"

    def initialize(self, config: Dict[str, Any]) -> None:
        """Initialize code execution scenario"""
        self.code = config.get("code", "")
        self.script = config.get("script", "")
        self.timeout = config.get("timeout", 300)
        self._config = config

        if not self.code and not self.script:
            raise ValueError("Code or script is required")

        logger.info("CodeExecutionScenario initialized")

    def run(self) -> Dict[str, Any]:
        """Execute code via mqs dispatch to remote execution server"""
        code_content = self.script or self.code
        scenario_id = self.context.scenario_id

        event_bus.emit("scenario.code_execution_started", {
            "scenario_id": scenario_id,
            "has_code": bool(self.code),
            "has_script": bool(self.script),
        })

        try:
            exec_role = "Python计算专家"
            exec_prompt = "你是一个专业的代码执行助手。请使用bash工具执行提供的代码，并返回执行结果。"
            exec_server_id = ""

            agent_roles = self._config.get("agent_roles", {})
            exec_agents = agent_roles.get("execution_agents", [])
            if exec_agents:
                first = exec_agents[0]
                exec_role = first.get("role", exec_role)
                exec_server_id = first.get("server_id", "")

            task_id = str(uuid.uuid4())
            goal = f"执行以下Python代码并返回输出结果:\n{code_content}"

            task_context = {
                "role": exec_role,
                "system_prompt": exec_prompt,
            }
            if exec_server_id:
                task_context["server_id"] = exec_server_id

            task = Task(
                task_id=task_id,
                parent_task_id="",
                topic_id=scenario_id,
                scenario_id=scenario_id,
                idempotency_key=f"{scenario_id}:{goal}",
                depends_on=None,
                goal=goal,
                state=TaskState.PENDING.value,
                priority=0,
                timeout_seconds=self.timeout,
                max_retries=0,
                retry_count=0,
                context=json.dumps(task_context, ensure_ascii=False),
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )

            task_repo = TaskRepository()
            task_repo.create(task)
            logger.info(f"Created code execution task: {task_id}")

            msg = TaskMessage(
                task_id=task_id,
                parent_task_id="",
                goal=goal,
                context=task_context,
            )
            mqs.dispatch_subtasks(scenario_id, [msg])
            logger.info(f"Dispatched code execution task via mqs: {task_id}")

            replies = mqs.collect_replies(scenario_id, 1, timeout=self.timeout)

            if not replies:
                task_repo.mark_as_failed(task_id, "No reply received within timeout")
                return {
                    "success": False,
                    "task_id": task_id,
                    "task_state": "timeout",
                    "error": "No reply received within timeout",
                }

            reply = replies[0]
            task_state = "success" if reply.success else "failed"

            logger.info(f"Code execution task completed: state={task_state}")

            event_bus.emit("scenario.code_execution_completed", {
                "scenario_id": scenario_id,
                "task_id": task_id,
                "task_state": task_state,
            })

            return {
                "success": reply.success,
                "task_id": task_id,
                "task_state": task_state,
                "output": reply.result,
            }

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Code execution scenario error: {error_msg}")

            event_bus.emit("scenario.code_execution_failed", {
                "scenario_id": scenario_id,
                "error": error_msg,
            })

            return {
                "success": False,
                "error": error_msg,
            }

    def cleanup(self) -> None:
        """Cleanup code execution scenario resources"""
        logger.info("CodeExecutionScenario cleaned up")
