"""
Tests for scenario timeout/failure routing in ScenarioManager._execute_scenario.

Regression: a scenario whose run() returns success=False (e.g. wait_for_task
timed out, returning state="timeout" without raising) used to be marked
COMPLETED. It must now be marked FAILED.
"""
import json
import uuid
from datetime import datetime

import pytest

from database.models.scenario import Scenario
from scenarios.scenario_manager import scenario_manager


class _StubScenario:
    """Duck-typed scenario: start() returns a canned result dict."""
    def __init__(self, result):
        self._result = result

    def start(self, config):
        return self._result


def _make_scenario(scenario_repo, state="running"):
    scn_id = f"scn-{uuid.uuid4().hex[:8]}"
    scenario_repo.create(Scenario(
        scenario_id=scn_id, scenario_type="simple_qa",
        name="t", description="", state=state,
        config=json.dumps({}),
        context=json.dumps({"trace_id": "trace-x"}),
        created_at=datetime.now(), updated_at=datetime.now(),
    ))
    return scn_id


class TestExecuteScenarioRouting:
    def test_timeout_result_routes_to_failed(self, scenario_repo):
        """run() returning success=False (timeout) -> scenario state 'failed'."""
        scn_id = _make_scenario(scenario_repo)
        stub = _StubScenario({
            "success": False,
            "task_state": "timeout",
            "error": "Task did not complete within timeout",
        })

        scenario_manager._execute_scenario(
            scn_id, stub, {"scenario_id": scn_id, "trace_id": "trace-x"})

        s = scenario_repo.find_by_scenario_id(scn_id)
        assert s.state == "failed"

    def test_success_result_routes_to_completed(self, scenario_repo):
        """run() returning success=True -> scenario state 'completed'."""
        scn_id = _make_scenario(scenario_repo)
        stub = _StubScenario({"success": True, "answer": "42"})

        scenario_manager._execute_scenario(
            scn_id, stub, {"scenario_id": scn_id, "trace_id": "trace-x"})

        s = scenario_repo.find_by_scenario_id(scn_id)
        assert s.state == "completed"

    def test_failed_task_result_routes_to_failed(self, scenario_repo):
        """run() returning success=False with a task failure -> 'failed'."""
        scn_id = _make_scenario(scenario_repo)
        stub = _StubScenario({
            "success": False,
            "task_state": "failed",
            "error": "agent raised",
        })

        scenario_manager._execute_scenario(
            scn_id, stub, {"scenario_id": scn_id, "trace_id": "trace-x"})

        assert scenario_repo.find_by_scenario_id(scn_id).state == "failed"

    def test_exception_routes_to_failed(self, scenario_repo):
        """start() raising -> 'failed' (existing behavior, unchanged)."""
        scn_id = _make_scenario(scenario_repo)

        class _Boom(_StubScenario):
            def start(self, config):
                raise RuntimeError("boom")

        scenario_manager._execute_scenario(
            scn_id, _Boom(None), {"scenario_id": scn_id, "trace_id": "trace-x"})

        assert scenario_repo.find_by_scenario_id(scn_id).state == "failed"

    def test_manual_acceptance_does_not_mark(self, scenario_repo):
        """manual_acceptance yields without marking state (coordinator owns it)."""
        scn_id = _make_scenario(scenario_repo)
        stub = _StubScenario({"success": False})  # would normally fail

        scenario_manager._execute_scenario(
            scn_id, stub,
            {"scenario_id": scn_id, "trace_id": "trace-x", "manual_acceptance": True})

        # State stays 'running' - coordinator advances via review cycle.
        assert scenario_repo.find_by_scenario_id(scn_id).state == "running"
