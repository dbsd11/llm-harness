"""
Unit tests for core/state_machine.py

Tests:
- TaskState enum values
- ScenarioState enum values
- StateMachine initialization, can_transition, transition
- TASK_STATE_MACHINE and SCENARIO_STATE_MACHINE transition tables
"""
import pytest

from core.state_machine import (
    StateMachine,
    TaskState,
    ScenarioState,
    TASK_STATE_MACHINE,
    SCENARIO_STATE_MACHINE,
)


class TestTaskStateEnum:
    def test_all_states_have_correct_values(self):
        assert TaskState.PENDING.value == "pending"
        assert TaskState.RUNNING.value == "running"
        assert TaskState.WAITING.value == "waiting"
        assert TaskState.SUCCESS.value == "success"
        assert TaskState.FAILED.value == "failed"
        assert TaskState.TIMEOUT.value == "timeout"
        assert TaskState.CANCELLED.value == "cancelled"

    def test_state_from_value(self):
        assert TaskState("pending") is TaskState.PENDING
        assert TaskState("running") is TaskState.RUNNING


class TestScenarioStateEnum:
    def test_all_states_have_correct_values(self):
        assert ScenarioState.INITIALIZING.value == "initializing"
        assert ScenarioState.RUNNING.value == "running"
        assert ScenarioState.COMPLETED.value == "completed"
        assert ScenarioState.FAILED.value == "failed"
        assert ScenarioState.CANCELLED.value == "cancelled"


class TestStateMachine:
    def test_initialize_sets_current_state(self):
        sm = StateMachine({TaskState.PENDING: {TaskState.RUNNING}})
        sm.initialize(TaskState.PENDING)
        assert sm.current_state == TaskState.PENDING

    def test_can_transition_returns_true_for_valid_transition(self):
        sm = StateMachine({TaskState.PENDING: {TaskState.RUNNING}})
        sm.initialize(TaskState.PENDING)
        assert sm.can_transition(TaskState.RUNNING) is True

    def test_can_transition_returns_false_for_invalid_transition(self):
        sm = StateMachine({TaskState.PENDING: {TaskState.RUNNING}})
        sm.initialize(TaskState.PENDING)
        assert sm.can_transition(TaskState.SUCCESS) is False

    def test_can_transition_returns_false_when_uninitialized(self):
        sm = StateMachine({TaskState.PENDING: {TaskState.RUNNING}})
        assert sm.can_transition(TaskState.RUNNING) is False

    def test_transition_succeeds_for_valid_target(self):
        sm = StateMachine({TaskState.PENDING: {TaskState.RUNNING}})
        sm.initialize(TaskState.PENDING)
        result = sm.transition(TaskState.RUNNING)
        assert result is True
        assert sm.current_state == TaskState.RUNNING

    def test_transition_fails_for_invalid_target(self):
        sm = StateMachine({TaskState.PENDING: {TaskState.RUNNING}})
        sm.initialize(TaskState.PENDING)
        result = sm.transition(TaskState.SUCCESS)
        assert result is False
        assert sm.current_state == TaskState.PENDING

    def test_transition_fails_when_uninitialized(self):
        sm = StateMachine({TaskState.PENDING: {TaskState.RUNNING}})
        result = sm.transition(TaskState.RUNNING)
        assert result is False
        assert sm.current_state is None

    def test_reinitialize_changes_state(self):
        sm = StateMachine({TaskState.PENDING: {TaskState.RUNNING}})
        sm.initialize(TaskState.PENDING)
        sm.transition(TaskState.RUNNING)
        sm.initialize(TaskState.PENDING)
        assert sm.current_state == TaskState.PENDING


class TestTaskStateMachine:
    """Tests the global TASK_STATE_MACHINE transition table."""

    def _fresh_sm(self):
        sm = StateMachine(TASK_STATE_MACHINE.transitions)
        return sm

    def test_pending_to_running(self):
        sm = self._fresh_sm()
        sm.initialize(TaskState.PENDING)
        assert sm.can_transition(TaskState.RUNNING)

    def test_pending_to_cancelled(self):
        sm = self._fresh_sm()
        sm.initialize(TaskState.PENDING)
        assert sm.can_transition(TaskState.CANCELLED)

    def test_pending_cannot_go_to_success(self):
        sm = self._fresh_sm()
        sm.initialize(TaskState.PENDING)
        assert not sm.can_transition(TaskState.SUCCESS)

    def test_running_to_success(self):
        sm = self._fresh_sm()
        sm.initialize(TaskState.RUNNING)
        assert sm.can_transition(TaskState.SUCCESS)

    def test_running_to_failed(self):
        sm = self._fresh_sm()
        sm.initialize(TaskState.RUNNING)
        assert sm.can_transition(TaskState.FAILED)

    def test_running_to_timeout(self):
        sm = self._fresh_sm()
        sm.initialize(TaskState.RUNNING)
        assert sm.can_transition(TaskState.TIMEOUT)

    def test_running_to_cancelled(self):
        sm = self._fresh_sm()
        sm.initialize(TaskState.RUNNING)
        assert sm.can_transition(TaskState.CANCELLED)

    def test_running_to_waiting(self):
        sm = self._fresh_sm()
        sm.initialize(TaskState.RUNNING)
        assert sm.can_transition(TaskState.WAITING)

    def test_waiting_to_running(self):
        sm = self._fresh_sm()
        sm.initialize(TaskState.WAITING)
        assert sm.can_transition(TaskState.RUNNING)

    def test_waiting_to_cancelled(self):
        sm = self._fresh_sm()
        sm.initialize(TaskState.WAITING)
        assert sm.can_transition(TaskState.CANCELLED)

    @pytest.mark.parametrize("terminal", [
        TaskState.SUCCESS, TaskState.FAILED,
        TaskState.TIMEOUT, TaskState.CANCELLED,
    ])
    def test_terminal_states_have_no_transitions(self, terminal):
        sm = self._fresh_sm()
        sm.initialize(terminal)
        for state in TaskState:
            assert not sm.can_transition(state)

    def test_full_lifecycle_pending_running_success(self):
        sm = self._fresh_sm()
        sm.initialize(TaskState.PENDING)
        assert sm.transition(TaskState.RUNNING)
        assert sm.transition(TaskState.SUCCESS)
        assert sm.current_state == TaskState.SUCCESS

    def test_full_lifecycle_with_retry(self):
        sm = self._fresh_sm()
        sm.initialize(TaskState.PENDING)
        assert sm.transition(TaskState.RUNNING)
        assert sm.transition(TaskState.WAITING)
        assert sm.transition(TaskState.RUNNING)
        assert sm.transition(TaskState.SUCCESS)

    def test_running_cannot_go_to_pending(self):
        sm = self._fresh_sm()
        sm.initialize(TaskState.RUNNING)
        assert not sm.can_transition(TaskState.PENDING)


class TestScenarioStateMachine:
    """Tests the global SCENARIO_STATE_MACHINE transition table."""

    def _fresh_sm(self):
        sm = StateMachine(SCENARIO_STATE_MACHINE.transitions)
        return sm

    def test_initializing_to_running(self):
        sm = self._fresh_sm()
        sm.initialize(ScenarioState.INITIALIZING)
        assert sm.can_transition(ScenarioState.RUNNING)

    def test_initializing_to_failed(self):
        sm = self._fresh_sm()
        sm.initialize(ScenarioState.INITIALIZING)
        assert sm.can_transition(ScenarioState.FAILED)

    def test_initializing_cannot_go_to_completed(self):
        sm = self._fresh_sm()
        sm.initialize(ScenarioState.INITIALIZING)
        assert not sm.can_transition(ScenarioState.COMPLETED)

    def test_running_to_completed(self):
        sm = self._fresh_sm()
        sm.initialize(ScenarioState.RUNNING)
        assert sm.can_transition(ScenarioState.COMPLETED)

    def test_running_to_failed(self):
        sm = self._fresh_sm()
        sm.initialize(ScenarioState.RUNNING)
        assert sm.can_transition(ScenarioState.FAILED)

    def test_running_to_cancelled(self):
        sm = self._fresh_sm()
        sm.initialize(ScenarioState.RUNNING)
        assert sm.can_transition(ScenarioState.CANCELLED)

    @pytest.mark.parametrize("terminal", [
        ScenarioState.COMPLETED, ScenarioState.FAILED,
    ])
    def test_terminal_states_have_no_transitions(self, terminal):
        sm = self._fresh_sm()
        sm.initialize(terminal)
        for state in ScenarioState:
            assert not sm.can_transition(state)

    def test_cancelled_can_restart_to_running(self):
        # A stopped (cancelled) scenario can be re-started from its config.
        sm = self._fresh_sm()
        sm.initialize(ScenarioState.CANCELLED)
        assert sm.can_transition(ScenarioState.RUNNING)

    def test_full_lifecycle(self):
        sm = self._fresh_sm()
        sm.initialize(ScenarioState.INITIALIZING)
        assert sm.transition(ScenarioState.RUNNING)
        assert sm.transition(ScenarioState.COMPLETED)
        assert sm.current_state == ScenarioState.COMPLETED
