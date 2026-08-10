# Code Execution Scenario - execute code in sandbox
from typing import Dict, Any
from scenarios.base_scenario import BaseScenario
from agents.agent_manager import agent_manager
from core.event_bus import event_bus
from logger import logger


class CodeExecutionScenario(BaseScenario):
    """
    Code Execution Scenario: Execute code in sandbox.

    Workflow:
    1. Receive code/script from config
    2. Create task for execution agent
    3. Execute in sandbox
    4. Return execution result
    """

    def get_scenario_type(self) -> str:
        return "code_execution"

    def initialize(self, config: Dict[str, Any]) -> None:
        """Initialize code execution scenario"""
        self.code = config.get("code", "")
        self.script = config.get("script", "")
        self.timeout = config.get("timeout", 300)

        if not self.code and not self.script:
            raise ValueError("Code or script is required")

        logger.info(f"CodeExecutionScenario initialized")

    def run(self) -> Dict[str, Any]:
        """Execute code workflow"""
        event_bus.emit("scenario.code_execution_started", {
            "scenario_id": self.context.scenario_id,
            "has_code": bool(self.code),
            "has_script": bool(self.script),
        })

        try:
            # Prepare context for execution agent
            context = {}
            if self.script:
                context["script"] = self.script
            elif self.code:
                context["script"] = self.code

            # Submit task to execution agent
            task_id = agent_manager.submit_task(
                goal="Execute code",
                agent_type="execution",
                scenario_id=self.context.scenario_id,
                timeout_seconds=self.timeout,
                context=context
            )

            logger.info(f"Submitted code execution task: {task_id}")

            # Wait for task completion and collect result
            task_result = self.wait_for_task(task_id, timeout=self.timeout)

            logger.info(f"Code execution task completed: state={task_result['state']}")

            event_bus.emit("scenario.code_execution_completed", {
                "scenario_id": self.context.scenario_id,
                "task_id": task_id,
                "task_state": task_result["state"],
            })

            return {
                "success": task_result["state"] == "success",
                "task_id": task_id,
                "task_state": task_result["state"],
                "output": task_result["result"],
                "error": task_result["error"],
            }

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Code execution scenario error: {error_msg}")

            event_bus.emit("scenario.code_execution_failed", {
                "scenario_id": self.context.scenario_id,
                "error": error_msg,
            })

            return {
                "success": False,
                "error": error_msg,
            }

    def cleanup(self) -> None:
        """Cleanup code execution scenario resources"""
        logger.info("CodeExecutionScenario cleaned up")
