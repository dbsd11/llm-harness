"""
Self-checks for the execution-server feature:
- SchedulingAgent._associate_role: associate a subtask with a configured role
- ExecutionServerRepository upsert (update, not duplicate insert)
- ws_protocol frame round-trip
- scenario start_guard + state machine restart rules
"""
import json
import pytest

from core.agents.scheduling_agent import SchedulingAgent
from core.state_machine import ScenarioState, SCENARIO_STATE_MACHINE
from database.repositories.execution_server_repository import ExecutionServerRepository
from core import ws_protocol as P
from pages.scenario_dashboard import start_guard


class TestAssociateRole:
    def _agent(self):
        return SchedulingAgent()

    def test_exact_match_uses_configured_definition_and_server(self):
        roles = [{"name": "数学家", "role": "你是数学专家", "server_id": "node-1"}]
        sub = {"context": {"role": "数学家", "system_prompt": "llm-generated"}}
        role, sys_prompt, sid = self._agent()._associate_role(sub, roles)
        assert role == "数学家"
        assert sys_prompt == "你是数学专家"  # configured definition overrides LLM
        assert sid == "node-1"

    def test_no_match_falls_back_to_first_configured_role(self):
        roles = [
            {"name": "数学家", "role": "你是数学专家", "server_id": "node-1"},
            {"name": "翻译官", "role": "你是翻译", "server_id": "node-2"},
        ]
        # LLM invented a name not in the configured list
        sub = {"context": {"role": "资深基础数学计算专家"}}
        role, sys_prompt, sid = self._agent()._associate_role(sub, roles)
        # Falls back to the first configured role so the task still routes.
        assert role == "数学家"
        assert sys_prompt == "你是数学专家"
        assert sid == "node-1"

    def test_no_server_configured_returns_none_server(self):
        roles = [{"name": "数学家", "role": "你是数学专家"}]  # no server_id
        sub = {"context": {"role": "数学家"}}
        role, sys_prompt, sid = self._agent()._associate_role(sub, roles)
        assert role == "数学家"
        assert sys_prompt == "你是数学专家"
        assert sid is None  # local execution

    def test_no_configured_roles_passes_through_llm_values(self):
        sub = {"context": {"role": "自定义角色", "system_prompt": "llm-prompt"}}
        role, sys_prompt, sid = self._agent()._associate_role(sub, [])
        assert role == "自定义角色"
        assert sys_prompt == "llm-prompt"
        assert sid is None


class TestStartGuard:
    def test_initializing_is_startable(self):
        blocked, _ = start_guard({"state": "initializing"})
        assert blocked is False

    def test_cancelled_stopped_is_startable(self):
        blocked, _ = start_guard({"state": "cancelled"})
        assert blocked is False

    def test_running_is_blocked(self):
        blocked, msg = start_guard({"state": "running"})
        assert blocked is True
        assert "运行" in msg

    def test_completed_is_blocked(self):
        blocked, msg = start_guard({"state": "completed"})
        assert blocked is True
        assert "结束" in msg or "无法" in msg

    def test_failed_is_blocked(self):
        blocked, _ = start_guard({"state": "failed"})
        assert blocked is True

    def test_missing_status_blocked(self):
        blocked, msg = start_guard(None)
        assert blocked is True


class TestScenarioRestartTransition:
    def test_cancelled_can_transition_to_running(self):
        sm = SCENARIO_STATE_MACHINE
        sm.initialize(ScenarioState.CANCELLED)
        assert sm.can_transition(ScenarioState.RUNNING) is True

    def test_completed_cannot_transition_to_running(self):
        sm = SCENARIO_STATE_MACHINE
        sm.initialize(ScenarioState.COMPLETED)
        assert sm.can_transition(ScenarioState.RUNNING) is False

    def test_initializing_can_transition_to_running(self):
        sm = SCENARIO_STATE_MACHINE
        sm.initialize(ScenarioState.INITIALIZING)
        assert sm.can_transition(ScenarioState.RUNNING) is True


class TestExecutionServerRepoUpsert:
    def test_upsert_updates_not_duplicates(self):
        repo = ExecutionServerRepository()
        repo.upsert("node-1", name="Node 1", total_quota=4,
                    env_info={"bash": True}, status="idle", connected=True)

        all_rows = repo.list_all()
        assert len(all_rows) == 1
        assert all_rows[0].server_id == "node-1"
        assert all_rows[0].total_quota == 4

        # Second upsert on same id updates fields, no new row.
        repo.upsert("node-1", name="Node 1 Renamed", total_quota=8,
                    env_info={"bash": False}, status="running", connected=True)
        all_rows = repo.list_all()
        assert len(all_rows) == 1
        assert all_rows[0].name == "Node 1 Renamed"
        assert all_rows[0].total_quota == 8

    def test_mark_offline(self):
        repo = ExecutionServerRepository()
        repo.upsert("node-2", name="Node 2", total_quota=2,
                    status="idle", connected=True)
        repo.mark_offline("node-2")
        row = repo.find_by_server_id("node-2")
        assert row.status == "offline"
        assert row.connected in (False, 0)


class TestExecutionServerRepoDelete:
    """Cleanup operations for the execution_servers registry."""

    def test_delete_offline_removes_only_disconnected(self):
        repo = ExecutionServerRepository()
        repo.upsert("node-1", name="Node 1", total_quota=4,
                    status="idle", connected=True)
        repo.upsert("node-2", name="Node 2", total_quota=2,
                    status="offline", connected=False)

        n = repo.delete_offline()

        assert n == 1
        remaining = {s.server_id for s in repo.list_all()}
        assert remaining == {"node-1"}  # connected row preserved

    def test_delete_offline_returns_zero_when_none(self):
        repo = ExecutionServerRepository()
        repo.upsert("node-1", name="Node 1", total_quota=4,
                    status="idle", connected=True)
        assert repo.delete_offline() == 0

    def test_delete_single_offline_row(self):
        repo = ExecutionServerRepository()
        repo.upsert("node-2", name="Node 2", total_quota=2,
                    status="offline", connected=False)

        assert repo.delete("node-2") is True
        assert repo.find_by_server_id("node-2") is None

    def test_delete_missing_returns_false(self):
        repo = ExecutionServerRepository()
        assert repo.delete("does-not-exist") is False


class TestWsProtocol:
    def test_round_trip(self):
        for raw in (
            P.register_frame("node-1", "Node 1", 4, {"bash": True, "sed": False}),
            P.status_frame(P.STATUS_RUNNING, 4, 2, {"bash": True}),
            P.task_event_frame("t1", P.EVENT_AGENT_CREATED, role="数学家"),
            P.task_result_frame("t1", True, {"output": "42"}),
            P.task_frame("t1", "p1", "calc", {"role": "数学家"}),
            P.ack_frame(True, task_id="t1"),
        ):
            f = P.parse_frame(raw)
            assert "type" in f and "payload" in f
            again = P.make_frame(f["type"], f["payload"], f["task_id"])
            assert P.parse_frame(again)["payload"] == f["payload"]
