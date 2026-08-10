# State machine engine
from enum import Enum
from typing import Dict, Set, Optional


class TaskState(Enum):
    """Task state machine states"""
    PENDING = "pending"          # Created, not started
    RUNNING = "running"          # Currently executing
    WAITING = "waiting"          # Waiting for dependency/event
    PENDING_REVIEW = "pending_review"  # Executed; awaiting human acceptance (manual_acceptance)
    SUCCESS = "success"          # Completed successfully
    FAILED = "failed"            # Failed (terminal)
    TIMEOUT = "timeout"          # Timeout (terminal)
    CANCELLED = "cancelled"      # Cancelled (terminal)


class ScenarioState(Enum):
    """Scenario state machine states"""
    INITIALIZING = "initializing"  # Setting up context + agent env
    RUNNING = "running"            # Agents executing
    AWAITING_REVIEW = "awaiting_review"  # Paused for human acceptance (manual_acceptance)
    COMPLETED = "completed"        # Successfully finished
    FAILED = "failed"              # Failed (terminal)
    CANCELLED = "cancelled"        # Cancelled (terminal)


class StateMachine:
    """Simple state machine with transition validation"""

    def __init__(self, transitions: Dict[Enum, Set[Enum]]):
        """
        Initialize state machine with transition table

        Args:
            transitions: Dict mapping state to set of allowed next states
        """
        self.transitions = transitions
        self.current_state: Optional[Enum] = None

    def initialize(self, initial_state: Enum):
        """Initialize the state machine with an initial state"""
        self.current_state = initial_state

    def can_transition(self, new_state: Enum) -> bool:
        """Check if transition to new state is allowed"""
        if self.current_state is None:
            return False
        allowed = self.transitions.get(self.current_state, set())
        return new_state in allowed

    def transition(self, new_state: Enum) -> bool:
        """
        Attempt to transition to new state

        Returns:
            True if transition succeeded, False if not allowed
        """
        if not self.can_transition(new_state):
            return False
        self.current_state = new_state
        return True


# Task state machine definition
TASK_STATE_MACHINE = StateMachine({
    TaskState.PENDING: {TaskState.RUNNING, TaskState.CANCELLED},
    TaskState.RUNNING: {TaskState.SUCCESS, TaskState.FAILED, TaskState.TIMEOUT, TaskState.CANCELLED, TaskState.WAITING, TaskState.PENDING_REVIEW},
    TaskState.WAITING: {TaskState.RUNNING, TaskState.CANCELLED},
    # Executed; gated for human acceptance. Pass -> SUCCESS, fail -> FAILED, stop -> CANCELLED.
    TaskState.PENDING_REVIEW: {TaskState.SUCCESS, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.SUCCESS: set(),   # Terminal state - no transitions out
    TaskState.FAILED: set(),    # Terminal state - no transitions out
    TaskState.TIMEOUT: set(),   # Terminal state - no transitions out
    TaskState.CANCELLED: set(), # Terminal state - no transitions out
})

# Scenario state machine definition
SCENARIO_STATE_MACHINE = StateMachine({
    ScenarioState.INITIALIZING: {ScenarioState.RUNNING, ScenarioState.FAILED},
    ScenarioState.RUNNING: {ScenarioState.COMPLETED, ScenarioState.FAILED, ScenarioState.CANCELLED, ScenarioState.AWAITING_REVIEW},
    # Paused at the acceptance gate. Next cycle -> RUNNING, all-passed -> COMPLETED.
    ScenarioState.AWAITING_REVIEW: {ScenarioState.RUNNING, ScenarioState.COMPLETED, ScenarioState.FAILED, ScenarioState.CANCELLED},
    ScenarioState.COMPLETED: set(),  # Terminal state
    ScenarioState.FAILED: set(),     # Terminal state
    # A stopped scenario can be restarted (re-run from its config).
    ScenarioState.CANCELLED: {ScenarioState.RUNNING},
})


# Lifecycle terminal task states - no further transitions out.
TASK_TERMINAL_STATES = {"success", "failed", "timeout", "cancelled"}

# Execution-complete task states - a redelivery of a task in one of these must
# NOT be re-executed. Superset of terminal: PENDING_REVIEW is done executing but
# not lifecycle-terminal (it can still be accepted/rejected by a human).
TASK_EXECUTION_COMPLETE_STATES = TASK_TERMINAL_STATES | {"pending_review"}
