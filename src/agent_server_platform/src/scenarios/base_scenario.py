# Base Scenario - scenario interface and context management
import json
import time
from typing import Any, Dict, List, Optional
from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from core.state_machine import TASK_TERMINAL_STATES


class ScenarioState(Enum):
    """Scenario state machine states"""
    INITIALIZING = "initializing"  # Setting up context + agent env
    RUNNING = "running"            # Agents executing
    COMPLETED = "completed"        # Successfully finished
    FAILED = "failed"              # Failed (terminal)
    CANCELLED = "cancelled"        # Cancelled (terminal)


class ScenarioContext:
    """Scenario execution context"""

    def __init__(self, scenario_id: str, config: Dict[str, Any] = None):
        self.scenario_id = scenario_id
        self.config = config or {}
        self.state = ScenarioState.INITIALIZING
        self.created_at = datetime.now()
        self.started_at: Optional[datetime] = None
        self.completed_at: Optional[datetime] = None
        self.result: Dict[str, Any] = {}
        self.error: Optional[str] = None
        self.metadata: Dict[str, Any] = {}

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "scenario_id": self.scenario_id,
            "state": self.state.value,
            "config": self.config,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "result": self.result,
            "error": self.error,
            "metadata": self.metadata,
        }


class AgentRole:
    """
    Defines an agent's role in a scenario (角色定义).

    Maps agent_type to specific responsibilities within a scenario.
    """

    def __init__(self, role_id: str, agent_type: str,
                 responsibilities: List[str] = None, config: Dict[str, Any] = None):
        self.role_id = role_id
        self.agent_type = agent_type  # "scheduling", "execution", or custom
        self.responsibilities = responsibilities or []
        self.config = config or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role_id": self.role_id,
            "agent_type": self.agent_type,
            "responsibilities": self.responsibilities,
            "config": self.config,
        }


class BaseScenario(ABC):
    """Base interface for all scenarios (场景定义)"""

    def __init__(self):
        self.context: Optional[ScenarioContext] = None

    @abstractmethod
    def get_scenario_type(self) -> str:
        """Return scenario type identifier"""
        pass

    def define_roles(self) -> List[AgentRole]:
        """
        Define agent roles for this scenario (角色定义).

        Override in subclasses to declare which agents participate
        and what they're responsible for. Default: no roles.
        """
        return []

    @abstractmethod
    def initialize(self, config: Dict[str, Any]) -> None:
        """Initialize scenario with configuration"""
        pass

    @abstractmethod
    def run(self) -> Dict[str, Any]:
        """Execute scenario logic, return result"""
        pass

    @abstractmethod
    def cleanup(self) -> None:
        """Cleanup resources"""
        pass

    def start(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Start scenario execution.

        Args:
            config: Scenario configuration

        Returns:
            Execution result
        """
        try:
            # Create context
            scenario_id = config.get("scenario_id", "unknown")
            self.context = ScenarioContext(scenario_id, config)

            # Initialize
            self.initialize(config)
            self.context.state = ScenarioState.RUNNING
            self.context.started_at = datetime.now()

            # Execute
            result = self.run()
            self.context.result = result
            self.context.state = ScenarioState.COMPLETED
            self.context.completed_at = datetime.now()

            return result

        except Exception as e:
            self.context.error = str(e)
            self.context.state = ScenarioState.FAILED
            self.context.completed_at = datetime.now()
            raise

        finally:
            # Cleanup
            try:
                self.cleanup()
            except Exception as e:
                pass  # Ignore cleanup errors

    def wait_for_task(self, task_id: str, timeout: int = 300) -> Dict[str, Any]:
        """
        Poll task until completion or timeout.

        ponytail: polling is the simplest correct approach. Evolve to event
        subscription (EventBus.subscribe) when latency or scale matters.

        Args:
            task_id: Task ID to wait for
            timeout: Max seconds to wait

        Returns:
            Dict with state, result, error
        """
        from database.repositories.task_repository import TaskRepository

        task_repo = TaskRepository()
        terminal_states = TASK_TERMINAL_STATES
        deadline = time.time() + timeout

        while time.time() < deadline:
            task = task_repo.find_by_task_id(task_id)
            if task and task.state in terminal_states:
                return {
                    "state": task.state,
                    "result": json.loads(task.result) if task.result else None,
                    "error": task.error,
                }
            time.sleep(1)

        return {"state": "timeout", "result": None, "error": "Task did not complete within timeout"}
