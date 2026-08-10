"""
Tests for the manual acceptance (人工验收) feature.

Covers:
- Phase 2.4: Repository layer (mark_as_pending_review, mark_as_reviewed, etc.)
- State machine transitions (PENDING_REVIEW, AWAITING_REVIEW)
- ScenarioManager coordination (review_task, advance_review_cycle, on_cycle_paused)
- Reverse dependency lookup (find_dependents_by_task_id)
"""
import pytest
import uuid
import json
from datetime import datetime

from core.state_machine import (
    TaskState, ScenarioState,
    TASK_TERMINAL_STATES, TASK_EXECUTION_COMPLETE_STATES,
    TASK_STATE_MACHINE, SCENARIO_STATE_MACHINE,
)
from database.repositories.task_repository import TaskRepository
from database.models.task import Task
from database.models.scenario import Scenario


def _make_task(task_id=None, scenario_id=None, state=TaskState.RUNNING.value, **kw):
    return Task(
        task_id=task_id or str(uuid.uuid4()),
        goal="Test task",
        state=state,
        priority=0,
        scenario_id=scenario_id,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        **kw,
    )


class TestTaskStateDefinitions:
    """Phase 1: state constants and terminal sets."""

    def test_pending_review_state_exists(self):
        assert TaskState.PENDING_REVIEW.value == "pending_review"

    def test_awaiting_review_state_exists(self):
        assert ScenarioState.AWAITING_REVIEW.value == "awaiting_review"

    def test_terminal_states_do_not_include_pending_review(self):
        assert "pending_review" not in TASK_TERMINAL_STATES
        assert "success" in TASK_TERMINAL_STATES
        assert "failed" in TASK_TERMINAL_STATES

    def test_execution_complete_is_superset_of_terminal(self):
        assert TASK_TERMINAL_STATES.issubset(TASK_EXECUTION_COMPLETE_STATES)
        assert "pending_review" in TASK_EXECUTION_COMPLETE_STATES


class TestTaskStateMachineTransitions:
    """Phase 1: PENDING_REVIEW transitions in the state machine."""

    def test_running_can_transition_to_pending_review(self):
        assert TASK_STATE_MACHINE.transitions[TaskState.RUNNING] & {TaskState.PENDING_REVIEW}

    def test_pending_review_can_transition_to_success(self):
        assert TaskState.SUCCESS in TASK_STATE_MACHINE.transitions[TaskState.PENDING_REVIEW]

    def test_pending_review_can_transition_to_failed(self):
        assert TaskState.FAILED in TASK_STATE_MACHINE.transitions[TaskState.PENDING_REVIEW]

    def test_pending_review_can_transition_to_cancelled(self):
        assert TaskState.CANCELLED in TASK_STATE_MACHINE.transitions[TaskState.PENDING_REVIEW]

    def test_pending_review_cannot_transition_to_running(self):
        assert TaskState.RUNNING not in TASK_STATE_MACHINE.transitions[TaskState.PENDING_REVIEW]

    def test_awaiting_review_can_transition_to_running(self):
        assert ScenarioState.RUNNING in SCENARIO_STATE_MACHINE.transitions[ScenarioState.AWAITING_REVIEW]

    def test_awaiting_review_can_transition_to_completed(self):
        assert ScenarioState.COMPLETED in SCENARIO_STATE_MACHINE.transitions[ScenarioState.AWAITING_REVIEW]

    def test_running_can_transition_to_awaiting_review(self):
        assert ScenarioState.AWAITING_REVIEW in SCENARIO_STATE_MACHINE.transitions[ScenarioState.RUNNING]


class TestMarkAsPendingReview:
    """Phase 2: mark_as_pending_review repository method."""

    def test_running_to_pending_review(self, task_repo):
        tid = f"t-{uuid.uuid4().hex[:8]}"
        task_repo.create(_make_task(task_id=tid, state=TaskState.RUNNING.value))

        ok = task_repo.mark_as_pending_review(tid, result='{"output": "done"}')
        assert ok is True

        task = task_repo.find_by_task_id(tid)
        assert task.state == TaskState.PENDING_REVIEW.value
        assert task.result == '{"output": "done"}'

    def test_idempotent_when_already_pending_review(self, task_repo):
        tid = f"t-{uuid.uuid4().hex[:8]}"
        task_repo.create(_make_task(task_id=tid, state=TaskState.RUNNING.value))
        task_repo.mark_as_pending_review(tid)

        ok = task_repo.mark_as_pending_review(tid)
        assert ok is True

    def test_preserves_agent_info(self, task_repo):
        tid = f"t-{uuid.uuid4().hex[:8]}"
        task_repo.create(_make_task(task_id=tid, state=TaskState.RUNNING.value))

        task_repo.mark_as_pending_review(
            tid, agent_name="ExecutionServer:s1", agent_role="translator",
            execution_duration=1.23)

        task = task_repo.find_by_task_id(tid)
        assert task.agent_name == "ExecutionServer:s1"
        assert task.agent_role == "translator"
        assert task.execution_duration == 1.23

    def test_returns_false_for_nonexistent_task(self, task_repo):
        ok = task_repo.mark_as_pending_review("nonexistent-task-id")
        assert ok is False


class TestMarkAsReviewed:
    """Phase 2: mark_as_reviewed — pass/fail with feedback."""

    def test_pass_sets_success(self, task_repo):
        tid = f"t-{uuid.uuid4().hex[:8]}"
        task_repo.create(_make_task(task_id=tid, state=TaskState.RUNNING.value))
        task_repo.mark_as_pending_review(tid)

        ok = task_repo.mark_as_reviewed(tid, passed=True, feedback="LGTM")
        assert ok is True

        task = task_repo.find_by_task_id(tid)
        assert task.state == TaskState.SUCCESS.value
        assert task.review_feedback == "LGTM"
        assert task.completed_at is not None

    def test_pass_without_feedback(self, task_repo):
        tid = f"t-{uuid.uuid4().hex[:8]}"
        task_repo.create(_make_task(task_id=tid, state=TaskState.RUNNING.value))
        task_repo.mark_as_pending_review(tid)

        ok = task_repo.mark_as_reviewed(tid, passed=True)
        assert ok is True

        task = task_repo.find_by_task_id(tid)
        assert task.state == TaskState.SUCCESS.value
        assert task.review_feedback is None

    def test_fail_sets_failed_with_feedback(self, task_repo):
        tid = f"t-{uuid.uuid4().hex[:8]}"
        task_repo.create(_make_task(task_id=tid, state=TaskState.RUNNING.value))
        task_repo.mark_as_pending_review(tid)

        ok = task_repo.mark_as_reviewed(tid, passed=False, feedback="翻译质量不达标")
        assert ok is True

        task = task_repo.find_by_task_id(tid)
        assert task.state == TaskState.FAILED.value
        assert task.error == "Manual rejection"
        assert task.review_feedback == "翻译质量不达标"
        assert task.completed_at is not None

    def test_fail_without_feedback_rejected(self, task_repo):
        tid = f"t-{uuid.uuid4().hex[:8]}"
        task_repo.create(_make_task(task_id=tid, state=TaskState.RUNNING.value))
        task_repo.mark_as_pending_review(tid)

        ok = task_repo.mark_as_reviewed(tid, passed=False)
        assert ok is False

        task = task_repo.find_by_task_id(tid)
        assert task.state == TaskState.PENDING_REVIEW.value

    def test_fail_with_empty_feedback_rejected(self, task_repo):
        tid = f"t-{uuid.uuid4().hex[:8]}"
        task_repo.create(_make_task(task_id=tid, state=TaskState.RUNNING.value))
        task_repo.mark_as_pending_review(tid)

        ok = task_repo.mark_as_reviewed(tid, passed=False, feedback="   ")
        assert ok is False

    def test_review_non_pending_review_task_rejected(self, task_repo):
        tid = f"t-{uuid.uuid4().hex[:8]}"
        task_repo.create(_make_task(task_id=tid, state=TaskState.RUNNING.value))

        ok = task_repo.mark_as_reviewed(tid, passed=True)
        assert ok is False

        task = task_repo.find_by_task_id(tid)
        assert task.state == TaskState.RUNNING.value

    def test_review_nonexistent_task_returns_false(self, task_repo):
        ok = task_repo.mark_as_reviewed("nonexistent", passed=True)
        assert ok is False


class TestPendingReviewQueries:
    """Phase 2: find/count pending review by scenario."""

    def test_find_pending_review_by_scenario(self, task_repo):
        sid = f"s-{uuid.uuid4().hex[:8]}"
        t1 = f"t1-{uuid.uuid4().hex[:8]}"
        t2 = f"t2-{uuid.uuid4().hex[:8]}"
        t3 = f"t3-{uuid.uuid4().hex[:8]}"

        task_repo.create(_make_task(task_id=t1, scenario_id=sid, state=TaskState.RUNNING.value))
        task_repo.create(_make_task(task_id=t2, scenario_id=sid, state=TaskState.RUNNING.value))
        task_repo.create(_make_task(task_id=t3, scenario_id=sid, state=TaskState.RUNNING.value))

        task_repo.mark_as_pending_review(t1)
        task_repo.mark_as_pending_review(t2)

        results = task_repo.find_pending_review_by_scenario(sid)
        assert len(results) == 2
        ids = {t.task_id for t in results}
        assert ids == {t1, t2}

    def test_count_pending_review_by_scenario(self, task_repo):
        sid = f"s-{uuid.uuid4().hex[:8]}"
        t1 = f"t1-{uuid.uuid4().hex[:8]}"
        t2 = f"t2-{uuid.uuid4().hex[:8]}"

        task_repo.create(_make_task(task_id=t1, scenario_id=sid, state=TaskState.RUNNING.value))
        task_repo.create(_make_task(task_id=t2, scenario_id=sid, state=TaskState.RUNNING.value))

        task_repo.mark_as_pending_review(t1)

        assert task_repo.count_pending_review_by_scenario(sid) == 1

        task_repo.mark_as_pending_review(t2)
        assert task_repo.count_pending_review_by_scenario(sid) == 2

    def test_empty_scenario_id_returns_empty(self, task_repo):
        assert task_repo.find_pending_review_by_scenario("") == []
        assert task_repo.count_pending_review_by_scenario("") == 0
        assert task_repo.count_pending_review_by_scenario(None) == 0


class TestFindDependentsByTaskId:
    """Phase 5: reverse dependency lookup for sub-tree re-derivation."""

    def test_finds_direct_dependents(self, task_repo):
        root_id = f"root-{uuid.uuid4().hex[:8]}"
        child_id = f"child-{uuid.uuid4().hex[:8]}"

        task_repo.create(_make_task(task_id=root_id, state=TaskState.SUCCESS.value))
        task_repo.create(_make_task(
            task_id=child_id, state=TaskState.PENDING.value,
            depends_on=json.dumps([root_id]),
        ))

        dependents = task_repo.find_dependents_by_task_id(root_id)
        assert len(dependents) == 1
        assert dependents[0].task_id == child_id

    def test_does_not_return_unrelated_tasks(self, task_repo):
        root_id = f"root-{uuid.uuid4().hex[:8]}"
        other_id = f"other-{uuid.uuid4().hex[:8]}"

        task_repo.create(_make_task(task_id=root_id, state=TaskState.SUCCESS.value))
        task_repo.create(_make_task(
            task_id=other_id, state=TaskState.PENDING.value,
            depends_on=json.dumps(["some-other-id"]),
        ))

        dependents = task_repo.find_dependents_by_task_id(root_id)
        assert len(dependents) == 0

    def test_empty_task_id_returns_empty(self, task_repo):
        assert task_repo.find_dependents_by_task_id("") == []
        assert task_repo.find_dependents_by_task_id(None) == []


class TestMarkAsCancelled:
    """Phase 5: cancel for superseded dependents during re-derivation."""

    def test_cancel_sets_terminal_state(self, task_repo):
        tid = f"t-{uuid.uuid4().hex[:8]}"
        task_repo.create(_make_task(task_id=tid, state=TaskState.PENDING.value))

        ok = task_repo.mark_as_cancelled(tid, reason="superseded by re-derivation")
        assert ok is True

        task = task_repo.find_by_task_id(tid)
        assert task.state == TaskState.CANCELLED.value
        assert task.error == "superseded by re-derivation"
        assert task.completed_at is not None


class TestScenarioManagerReviewCoordination:
    """Phase 6: ScenarioManager review_task, on_cycle_paused, advance_review_cycle."""

    def test_review_task_pass_emits_event(self, task_repo, scenario_repo, event_bus):
        from scenarios.scenario_manager import ScenarioManager

        mgr = ScenarioManager()
        sid = f"s-{uuid.uuid4().hex[:8]}"
        tid = f"t-{uuid.uuid4().hex[:8]}"

        scenario_repo.create(Scenario(
            scenario_id=sid, name="test", scenario_type="test",
            config='{"manual_acceptance": true}',
            state=ScenarioState.INITIALIZING.value,
            created_at=datetime.now(), updated_at=datetime.now(),
        ))
        scenario_repo.update_scenario_state(sid, ScenarioState.AWAITING_REVIEW.value)

        task_repo.create(_make_task(task_id=tid, scenario_id=sid, state=TaskState.RUNNING.value))
        task_repo.mark_as_pending_review(tid)

        received = []
        event_bus.subscribe("task.reviewed", lambda e: received.append(e))

        ok = mgr.review_task(tid, passed=True, feedback="good")
        assert ok is True

        task = task_repo.find_by_task_id(tid)
        assert task.state == TaskState.SUCCESS.value

        assert any(e["data"]["passed"] is True for e in received)

    def test_review_task_fail_triggers_advance(self, task_repo, scenario_repo, event_bus, monkeypatch):
        from scenarios.scenario_manager import ScenarioManager

        # Prevent background thread from agent_manager.submit_task
        monkeypatch.setattr("agents.agent_manager.agent_manager.submit_task", lambda *a, **kw: None)

        mgr = ScenarioManager()
        sid = f"s-{uuid.uuid4().hex[:8]}"
        tid = f"t-{uuid.uuid4().hex[:8]}"

        scenario_repo.create(Scenario(
            scenario_id=sid, name="test", scenario_type="test",
            config='{"manual_acceptance": true}',
            state=ScenarioState.INITIALIZING.value,
            created_at=datetime.now(), updated_at=datetime.now(),
        ))
        scenario_repo.update_scenario_state(sid, ScenarioState.AWAITING_REVIEW.value)

        task_repo.create(_make_task(task_id=tid, scenario_id=sid, state=TaskState.RUNNING.value))
        task_repo.mark_as_pending_review(tid)

        ok = mgr.review_task(tid, passed=False, feedback="需要改进")
        assert ok is True

        task = task_repo.find_by_task_id(tid)
        assert task.state == TaskState.FAILED.value
        assert task.review_feedback == "需要改进"

    def test_on_cycle_paused_transitions_to_awaiting_review(self, scenario_repo, event_bus):
        from scenarios.scenario_manager import ScenarioManager

        mgr = ScenarioManager()
        sid = f"s-{uuid.uuid4().hex[:8]}"

        scenario_repo.create(Scenario(
            scenario_id=sid, name="test", scenario_type="test",
            config='{}',
            state=ScenarioState.INITIALIZING.value,
            created_at=datetime.now(), updated_at=datetime.now(),
        ))
        scenario_repo.update_scenario_state(sid, ScenarioState.RUNNING.value)

        received = []
        event_bus.subscribe("scenario.awaiting_review", lambda e: received.append(e))

        mgr.on_cycle_paused(sid)

        scenario = scenario_repo.find_by_scenario_id(sid)
        assert scenario.state == ScenarioState.AWAITING_REVIEW.value
        assert len(received) >= 1

    def test_on_cycle_paused_noop_if_not_running(self, scenario_repo):
        from scenarios.scenario_manager import ScenarioManager

        mgr = ScenarioManager()
        sid = f"s-{uuid.uuid4().hex[:8]}"

        scenario_repo.create(Scenario(
            scenario_id=sid, name="test", scenario_type="test",
            config='{}',
            state=ScenarioState.INITIALIZING.value,
            created_at=datetime.now(), updated_at=datetime.now(),
        ))
        scenario_repo.update_scenario_state(sid, ScenarioState.INITIALIZING.value)

        mgr.on_cycle_paused(sid)

        scenario = scenario_repo.find_by_scenario_id(sid)
        assert scenario.state == ScenarioState.INITIALIZING.value

    def test_advance_review_cycle_completes_when_all_pass(self, task_repo, scenario_repo):
        from scenarios.scenario_manager import ScenarioManager

        mgr = ScenarioManager()
        sid = f"s-{uuid.uuid4().hex[:8]}"
        tid = f"t-{uuid.uuid4().hex[:8]}"

        scenario_repo.create(Scenario(
            scenario_id=sid, name="test", scenario_type="test",
            config='{"manual_acceptance": true}',
            state=ScenarioState.INITIALIZING.value,
            created_at=datetime.now(), updated_at=datetime.now(),
        ))
        scenario_repo.update_scenario_state(sid, ScenarioState.AWAITING_REVIEW.value)

        task_repo.create(_make_task(task_id=tid, scenario_id=sid, state=TaskState.SUCCESS.value))

        mgr.advance_review_cycle(sid)

        scenario = scenario_repo.find_by_scenario_id(sid)
        assert scenario.state == ScenarioState.COMPLETED.value

    def test_advance_review_cycle_resumes_when_failed_with_feedback(
            self, task_repo, scenario_repo, monkeypatch):
        from scenarios.scenario_manager import ScenarioManager

        # Prevent background thread from agent_manager.submit_task
        monkeypatch.setattr("agents.agent_manager.agent_manager.submit_task", lambda *a, **kw: None)

        mgr = ScenarioManager()
        sid = f"s-{uuid.uuid4().hex[:8]}"
        tid = f"t-{uuid.uuid4().hex[:8]}"

        scenario_repo.create(Scenario(
            scenario_id=sid, name="test", scenario_type="test",
            config='{"manual_acceptance": true}',
            state=ScenarioState.INITIALIZING.value,
            created_at=datetime.now(), updated_at=datetime.now(),
        ))
        scenario_repo.update_scenario_state(sid, ScenarioState.AWAITING_REVIEW.value)

        task_repo.create(_make_task(
            task_id=tid, scenario_id=sid, state=TaskState.FAILED.value,
            review_feedback="needs improvement",
        ))

        mgr.advance_review_cycle(sid)

        scenario = scenario_repo.find_by_scenario_id(sid)
        assert scenario.state == ScenarioState.RUNNING.value

    def test_advance_review_cycle_noop_if_pending_review_remains(
            self, task_repo, scenario_repo):
        from scenarios.scenario_manager import ScenarioManager

        mgr = ScenarioManager()
        sid = f"s-{uuid.uuid4().hex[:8]}"
        tid = f"t-{uuid.uuid4().hex[:8]}"

        scenario_repo.create(Scenario(
            scenario_id=sid, name="test", scenario_type="test",
            config='{"manual_acceptance": true}',
            state=ScenarioState.INITIALIZING.value,
            created_at=datetime.now(), updated_at=datetime.now(),
        ))
        scenario_repo.update_scenario_state(sid, ScenarioState.AWAITING_REVIEW.value)

        task_repo.create(_make_task(task_id=tid, scenario_id=sid, state=TaskState.RUNNING.value))
        task_repo.mark_as_pending_review(tid)

        mgr.advance_review_cycle(sid)

        scenario = scenario_repo.find_by_scenario_id(sid)
        assert scenario.state == ScenarioState.AWAITING_REVIEW.value

    def test_advance_review_cycle_noop_if_not_awaiting_review(
            self, scenario_repo):
        from scenarios.scenario_manager import ScenarioManager

        mgr = ScenarioManager()
        sid = f"s-{uuid.uuid4().hex[:8]}"

        scenario_repo.create(Scenario(
            scenario_id=sid, name="test", scenario_type="test",
            config='{}',
            state=ScenarioState.INITIALIZING.value,
            created_at=datetime.now(), updated_at=datetime.now(),
        ))
        scenario_repo.update_scenario_state(sid, ScenarioState.RUNNING.value)

        mgr.advance_review_cycle(sid)

        scenario = scenario_repo.find_by_scenario_id(sid)
        assert scenario.state == ScenarioState.RUNNING.value
