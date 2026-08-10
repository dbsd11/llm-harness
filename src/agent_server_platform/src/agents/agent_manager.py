# Agent Manager - agent registry and lifecycle management
import asyncio
import uuid
import json
import threading
from typing import Dict, Any, Optional
from datetime import datetime

from database.repositories.agent_repository import AgentRepository
from database.repositories.task_repository import TaskRepository
from database.models.task import Task
from core.agents.base_agent import BaseAgent, AgentRun
from core.agents.scheduling_agent import SchedulingAgent
from core.agents.execution_agent import ExecutionAgent
from core.state_machine import TaskState, TASK_TERMINAL_STATES
from core.event_bus import event_bus
from common.utils.global_loop_util import get_random_work_loop
from logger import logger


class AgentManager:
    """
    Agent Manager: Registry, lifecycle, and task submission.

    Responsibilities:
    - Agent registration and management
    - Agent run tracking
    - Task submission and execution
    - Agent lifecycle (start/stop/cleanup)
    """

    def __init__(self):
        self.agent_repo = AgentRepository()
        self.task_repo = TaskRepository()
        self.agent_runs: Dict[str, AgentRun] = {}
        # ponytail: store classes, not instances — fresh agent per scenario
        self.agent_registry: Dict[str, type] = {}
        self.lock = threading.Lock()

        # Register built-in agent classes
        self._register_builtin_agents()

    def _register_builtin_agents(self):
        """Register built-in agent classes ( instantiated per-task, not shared)"""
        self.agent_registry["scheduling"] = SchedulingAgent
        self.agent_registry["execution"] = ExecutionAgent

    def submit_task(self, goal: str, agent_type: str = "scheduling",
                   scenario_id: str = None, priority: int = 0,
                   timeout_seconds: int = 3600, context: Dict[str, Any] = None) -> str:
        """
        Submit a new task for execution.

        Args:
            goal: Task goal
            agent_type: Agent type to handle task
            scenario_id: Scenario ID (optional)
            priority: Task priority
            timeout_seconds: Task timeout
            context: Task context

        Returns:
            Task ID
        """
        task_id = str(uuid.uuid4())
        agent_run_id = str(uuid.uuid4())

        # Create task (with agent_run_id for cleanup tracking)
        task = Task(
            task_id=task_id,
            agent_run_id=agent_run_id,
            goal=goal,
            state=TaskState.PENDING.value,
            priority=priority,
            timeout_seconds=timeout_seconds,
            max_retries=3,
            retry_count=0,
            scenario_id=scenario_id,
            context=json.dumps(context or {}),
            created_at=datetime.now(),
            updated_at=datetime.now()
        )

        self.task_repo.create(task)

        # Create fresh agent instance for this task (not shared across scenarios)
        agent_cls = self.agent_registry.get(agent_type)
        if not agent_cls:
            logger.error(f"Agent type not found: {agent_type}")
            return task_id

        agent = agent_cls()
        agent.initialize(context or {})

        # Agent DB rows are now registered from the scenario's declared topology
        # at scenario start (scenario_manager._register_declared_agents), not as
        # a side-effect of task submission — the decoupled architecture runs
        # execution agents on remote exec-servers, so per-task rows here would
        # be incomplete (only scheduling, empty config) and misleading.

        agent_run = AgentRun(agent, task_id, context or {})

        with self.lock:
            self.agent_runs[agent_run_id] = agent_run

        # Submit to async execution
        work_loop = get_random_work_loop()
        asyncio.run_coroutine_threadsafe(self._execute_task(agent_run_id, agent_run), work_loop)

        event_bus.emit("task.submitted", {
            "task_id": task_id,
            "goal": goal,
            "agent_type": agent_type,
        })

        logger.info(f"Submitted task: {task_id} ({goal})")
        return task_id

    async def _execute_task(self, agent_run_id: str, agent_run: AgentRun):
        """Execute task asynchronously"""
        try:
            agent_run.start()

            # Mark task as running - ensure state is updated
            success = self.task_repo.mark_as_started(agent_run.task_id)
            if not success:
                logger.error(f"Failed to mark task as started: {agent_run.task_id}")
                agent_run.fail("Failed to mark task as started")
                return

            logger.info(f"Task marked as started: {agent_run.task_id}")
            event_bus.emit("task.started", {"task_id": agent_run.task_id})

            # Execute agent - use the context from agent_run
            task = self.task_repo.find_by_task_id(agent_run.task_id)
            context = agent_run.config.copy()
            context["goal"] = task.goal
            context["task_id"] = agent_run.task_id
            context["scenario_id"] = task.scenario_id

            # Inject agent_roles from the scenario config so the SchedulingAgent
            # can resolve per-role execution-server routing (server_id).
            if "agent_roles" not in context and task.scenario_id:
                try:
                    from database.repositories.scenario_repository import ScenarioRepository
                    scenario = ScenarioRepository().find_by_scenario_id(task.scenario_id)
                    if scenario and scenario.config:
                        scenario_config = json.loads(scenario.config)
                        context["agent_roles"] = scenario_config.get("agent_roles", {})
                except Exception as e:
                    logger.warning(f"Could not load agent_roles for scenario "
                                   f"{task.scenario_id}: {e}")

            # Run agent in thread pool to avoid blocking event loop
            # (SchedulingAgent may block on reply_queue.get())
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, agent_run.agent.run, agent_run.task_id, context
            )

            if result.get("success"):
                agent_run.complete(result)

                # Calculate execution duration
                execution_duration = None
                if agent_run.started_at and agent_run.completed_at:
                    execution_duration = (agent_run.completed_at - agent_run.started_at).total_seconds()

                # Get agent information
                agent_name = agent_run.agent.__class__.__name__
                agent_role = agent_run.agent.get_agent_type()

                self.task_repo.mark_as_completed(
                    agent_run.task_id,
                    json.dumps(result),
                    agent_name=agent_name,
                    agent_role=agent_role,
                    execution_duration=execution_duration
                )

                event_bus.emit("task.completed", {
                    "task_id": agent_run.task_id,
                    "result": result,
                })

                logger.info(f"Task completed: {agent_run.task_id}")

                # Check if this is a scheduling task that paused for review
                if result.get("paused_for_review") and task.scenario_id:
                    from scenarios.scenario_manager import scenario_manager
                    scenario_manager.on_cycle_paused(task.scenario_id)
            else:
                error = result.get("error", "Unknown error")
                agent_run.fail(error)
                self.task_repo.mark_as_failed(agent_run.task_id, error)

                event_bus.emit("task.failed", {
                    "task_id": agent_run.task_id,
                    "error": error,
                })

                logger.error(f"Task failed: {agent_run.task_id} - {error}")

            # Check if all subtasks in the topic are done (子主题完成追踪)
            self._check_topic_completion(agent_run.task_id)

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Task execution error: {error_msg}")

            agent_run.fail(error_msg)
            self.task_repo.mark_as_failed(agent_run.task_id, error_msg)

            event_bus.emit("task.failed", {
                "task_id": agent_run.task_id,
                "error": error_msg,
            })

        finally:
            # Cleanup agent run after completion
            # Keep in registry for polling, but mark as not processing
            pass

    def _check_topic_completion(self, task_id: str):
        """
        Check if all subtasks in a topic have reached terminal state.
        If so, emit topic.completed event (子主题结束 → 释放底层agent运行环境).
        """
        task = self.task_repo.find_by_task_id(task_id)
        if not task or not task.topic_id:
            return

        terminal_states = TASK_TERMINAL_STATES
        siblings = self.task_repo.find_by_topic_id(task.topic_id)

        if not siblings:
            return

        all_done = all(t.state in terminal_states for t in siblings)
        if all_done:
            success_count = sum(1 for t in siblings if t.state == "success")
            event_bus.emit("topic.completed", {
                "topic_id": task.topic_id,
                "total_subtasks": len(siblings),
                "success_count": success_count,
                "failed_count": len(siblings) - success_count,
            })
            logger.info(f"Topic {task.topic_id} completed: "
                        f"{success_count}/{len(siblings)} succeeded")

    def get_agent_run_status(self, agent_run_id: str) -> Optional[Dict[str, Any]]:
        """Get agent run status"""
        with self.lock:
            agent_run = self.agent_runs.get(agent_run_id)
            if agent_run:
                return agent_run.to_dict()
        return None

    def cleanup_agent_run(self, agent_run_id: str) -> bool:
        """Cleanup agent run from registry"""
        with self.lock:
            if agent_run_id in self.agent_runs:
                del self.agent_runs[agent_run_id]
                return True
        return False

    def list_agents(self) -> list:
        """List all registered agents"""
        return self.agent_repo.find_all()

    def list_agent_runs(self, limit: int = 100) -> list:
        """List recent agent runs"""
        with self.lock:
            runs = list(self.agent_runs.values())[:limit]
            return [run.to_dict() for run in runs]

    def shutdown(self):
        """Shutdown agent manager"""
        # Cleanup all active agent runs (the only place live instances exist)
        with self.lock:
            for agent_run in self.agent_runs.values():
                try:
                    agent_run.agent.cleanup()
                except Exception as e:
                    logger.error(f"Agent cleanup error during shutdown: {e}")
            self.agent_runs.clear()

        logger.info("AgentManager shutdown")


# Global agent manager instance
agent_manager = AgentManager()
