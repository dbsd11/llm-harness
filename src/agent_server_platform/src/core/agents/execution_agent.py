# Execution Agent - LLM-powered intelligent agent
import json
from typing import Dict, Any, List
from .base_agent import BaseAgent
from core.llm_client import llm_client
from core.event_bus import event_bus
from logger import logger


class ExecutionAgent(BaseAgent):
    """
    Execution Agent: LLM-powered intelligent agent with role and system prompt

    Responsibilities:
    - Role-based task execution using LLM
    - Intelligent Q&A based on system prompt
    - Context-aware response generation
    """

    def __init__(self):
        self.config = {}
        self.role = None
        self.system_prompt = None

    def get_agent_type(self) -> str:
        return "execution"

    def initialize(self, config: Dict[str, Any]) -> None:
        """
        Initialize execution agent with role and system prompt

        Args:
            config: Configuration dict containing:
                - role: Agent's role/expertise (e.g., "mathematical calculator")
                - system_prompt: System prompt defining capabilities and behavior
        """
        self.config = config
        self.role = config.get("role", "general assistant")
        self.system_prompt = config.get("system_prompt", "You are a helpful assistant.")
        logger.info(f"ExecutionAgent initialized with role: {self.role}")

    def run(self, task_id: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute task using LLM-based Q&A

        Flow: RECEIVE → PROCESS → RESPOND
        - Receive: get task/question from context
        - Process: use LLM with role and system prompt
        - Respond: return LLM's answer

        Args:
            task_id: Task identifier
            context: Task context containing 'goal' or 'question'

        Returns:
            Execution result with LLM response
        """
        event_bus.emit("task.execution_started", {"task_id": task_id})

        try:
            # Extract question/goal from context
            question = context.get("goal") or context.get("question", "")
            if not question:
                raise ValueError("No question or goal provided in context")

            logger.info(f"ExecutionAgent processing question: {question[:100]}...")

            # Build messages for LLM
            messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": question}
            ]

            # Call LLM
            response = llm_client.chat(messages, temperature=0.7)

            if response is None:
                raise RuntimeError("LLM returned empty response")

            # Emit success event
            event_bus.emit("task.execution_completed", {
                "task_id": task_id,
                "role": self.role,
                "question_length": len(question),
                "response_length": len(response),
            })

            return {
                "success": True,
                "output": response,
                "role": self.role,
                "question": question,
            }

        except Exception as e:
            error_msg = str(e)
            logger.error(f"ExecutionAgent error for task {task_id}: {error_msg}")

            # Emit failure event
            event_bus.emit("task.execution_failed", {
                "task_id": task_id,
                "error": error_msg,
            })

            return {
                "success": False,
                "output": "",
                "error": error_msg,
            }

    def cleanup(self) -> None:
        """Cleanup resources"""
        logger.info("ExecutionAgent cleaned up")
