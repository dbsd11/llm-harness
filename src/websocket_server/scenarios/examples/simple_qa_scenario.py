# Simple QA Scenario - basic question-answering workflow
from typing import Dict, Any
from scenarios.base_scenario import BaseScenario
from core.agents.agent_manager import agent_manager
from core.event_bus import event_bus
from logger import logger


class SimpleQAScenario(BaseScenario):
    """
    Simple QA Scenario: Basic question-answering workflow.

    Workflow:
    1. Receive question from config
    2. Create task for scheduling agent
    3. Wait for result
    4. Return answer
    """

    def get_scenario_type(self) -> str:
        return "simple_qa"

    def initialize(self, config: Dict[str, Any]) -> None:
        """Initialize QA scenario"""
        self.question = config.get("question", "")
        self.timeout = config.get("timeout", 180)

        if not self.question:
            raise ValueError("Question is required")

        logger.info(f"SimpleQAScenario initialized with question: {self.question}")

    def run(self) -> Dict[str, Any]:
        """Execute QA workflow"""
        _tid = self.context.config.get("tenant_id") if self.context.config else None
        event_bus.emit("scenario.qa_started", {
            "scenario_id": self.context.scenario_id,
            "question": self.question,
        }, tenant_id=_tid)

        try:
            # Submit task to scheduling agent
            task_id = agent_manager.submit_task(
                goal=self.question,
                agent_type="scheduling",
                scenario_id=self.context.scenario_id,
                timeout_seconds=self.timeout,
                context=self.context.config,  # ponytail: pass agent_roles so scheduler can route tasks
            )

            logger.info(f"Submitted QA task: {task_id}")

            # Wait for task completion and collect result
            task_result = self.wait_for_task(task_id, timeout=self.timeout)

            logger.info(f"QA task completed: state={task_result['state']}")

            event_bus.emit("scenario.qa_completed", {
                "scenario_id": self.context.scenario_id,
                "task_id": task_id,
                "task_state": task_result["state"],
            }, tenant_id=_tid)

            return {
                "success": task_result["state"] == "success",
                "task_id": task_id,
                "question": self.question,
                "answer": task_result["result"],
                "task_state": task_result["state"],
                "error": task_result["error"],
            }

        except Exception as e:
            error_msg = str(e)
            logger.error(f"QA scenario error: {error_msg}")

            event_bus.emit("scenario.qa_failed", {
                "scenario_id": self.context.scenario_id,
                "error": error_msg,
            }, tenant_id=_tid)

            return {
                "success": False,
                "error": error_msg,
            }

    def cleanup(self) -> None:
        """Cleanup QA scenario resources"""
        logger.info("SimpleQAScenario cleaned up")
