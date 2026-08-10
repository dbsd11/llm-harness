"""
Unit + integration tests for SchedulingAgent dependency / serial execution.

Covers:
- _build_waves: parallel, linear chain, diamond, cycle detection
- _inject_upstream: dependent task context gets predecessor results
- run() with a 2-step dependency chain: wave 2 sees wave 1's output, dispatched serially
- failure propagation: a failed predecessor skips its dependents (not dispatched)
"""
import json
import uuid
from datetime import datetime

import pytest

from core.agents.scheduling_agent import SchedulingAgent
from core.state_machine import TaskState
from database.repositories.message_repository import MessageRepository
from database.repositories.task_repository import TaskRepository
from database.models.task import Task


# --- helpers ----------------------------------------------------------------

def _sub(id_, goal, depends_on=None, role="数学家"):
    return {
        "id": id_,
        "goal": goal,
        "type": "execution",
        "priority": 0,
        "timeout_seconds": 60,
        "depends_on": depends_on or [],
        "context": {"role": role, "system_prompt": "s", "question": goal},
    }


def _create_task(task_repo, task_id, scenario_id="scn"):
    task_repo.create(Task(
        task_id=task_id, goal="g", state=TaskState.PENDING.value,
        scenario_id=scenario_id, context="{}",
        created_at=datetime.now(), updated_at=datetime.now(),
    ))


# --- _build_waves -----------------------------------------------------------

class TestBuildWaves:
    def _agent(self):
        return SchedulingAgent()

    def test_independent_tasks_single_wave(self):
        subs = [_sub("t1", "a"), _sub("t2", "b"), _sub("t3", "c")]
        waves = self._agent()._build_waves(subs)
        assert len(waves) == 1
        assert {s["id"] for s in waves[0]} == {"t1", "t2", "t3"}

    def test_linear_chain_three_waves(self):
        subs = [_sub("t1", "a"), _sub("t2", "b", ["t1"]), _sub("t3", "c", ["t2"])]
        waves = self._agent()._build_waves(subs)
        assert len(waves) == 3
        assert [s["id"] for s in waves[0]] == ["t1"]
        assert [s["id"] for s in waves[1]] == ["t2"]
        assert [s["id"] for s in waves[2]] == ["t3"]

    def test_diamond_three_waves(self):
        # t1 -> {t2, t3} -> t4
        subs = [
            _sub("t1", "a"),
            _sub("t2", "b", ["t1"]),
            _sub("t3", "c", ["t1"]),
            _sub("t4", "d", ["t2", "t3"]),
        ]
        waves = self._agent()._build_waves(subs)
        assert len(waves) == 3
        assert {s["id"] for s in waves[0]} == {"t1"}
        assert {s["id"] for s in waves[1]} == {"t2", "t3"}
        assert {s["id"] for s in waves[2]} == {"t4"}

    def test_cycle_raises(self):
        subs = [_sub("t1", "a", ["t2"]), _sub("t2", "b", ["t1"])]
        with pytest.raises(ValueError):
            self._agent()._build_waves(subs)

    def test_unknown_dependency_ref_dropped(self):
        # depends_on references a non-existent id -> dropped, task runs in wave 0
        subs = [_sub("t1", "a", ["ghost"])]
        waves = self._agent()._build_waves(subs)
        assert len(waves) == 1
        assert waves[0][0]["id"] == "t1"

    def test_self_dependency_dropped(self):
        subs = [_sub("t1", "a", ["t1"])]
        waves = self._agent()._build_waves(subs)
        assert len(waves) == 1
        assert waves[0][0]["id"] == "t1"


# --- _inject_upstream -------------------------------------------------------

class TestInjectUpstream:
    def test_injects_predecessor_results(self):
        agent = SchedulingAgent()
        ctx = {"role": "r", "system_prompt": "s", "question": "q"}
        # resolved_dep_ids are real task_ids (already mapped from local ids)
        replies = {"task-1": {"success": True, "output": "CODE-42"}}
        agent._inject_upstream(ctx, ["task-1"], replies)
        assert ctx["upstream_results"]["task-1"]["output"] == "CODE-42"
        assert ctx["upstream_outputs"] == ["CODE-42"]

    def test_no_deps_leaves_context_untouched(self):
        agent = SchedulingAgent()
        ctx = {"role": "r", "system_prompt": "s", "question": "q"}
        agent._inject_upstream(ctx, [], {})
        assert "upstream_results" not in ctx
        assert "upstream_outputs" not in ctx


# --- run() with a dependency chain (integration) ----------------------------
#
# These stub mqs.collect_replies to return synthetic replies per wave, so the
# wave-orchestration logic is tested without a live CentralDispatcher consumer
# (which isn't running in unit tests).

@pytest.fixture
def stub_chain_decompose(monkeypatch):
    """Make _decompose_goal return a 2-step chain: t1 -> t2."""
    from core import llm_client
    monkeypatch.setattr(llm_client.llm_client, "client", object())

    def _decompose(goal, context):
        return [
            _sub("t1", "write code", role="代码执行专家"),
            _sub("t2", "run code", depends_on=["t1"], role="代码执行专家"),
        ]
    monkeypatch.setattr(llm_client.llm_client, "decompose_goal", _decompose)
    return llm_client.llm_client


def _stub_collect_replies(monkeypatch, reply_fn):
    """Replace mqs.collect_replies with reply_fn(scenario_id, dispatched_ids) -> [ReplyMessage].

    reply_fn inspects the just-dispatched task_ids and returns synthetic replies,
    so wave N's output feeds wave N+1's context.
    """
    from core.message_queue import mqs, ReplyMessage

    def _collect(scenario_id, expected_count, timeout=300):
        # Read the dispatch rows written since the last collection to learn
        # which task_ids were just dispatched in this wave. Sort by id ASC
        # (find_by_scenario_id returns DESC) and take the last expected_count.
        rows = MessageRepository().find_by_scenario_id(scenario_id, 200)
        dispatched = sorted(
            ((r.id, r.task_id) for r in rows if r.message_type == "dispatch"),
            key=lambda x: x[0])
        ids = [t for _, t in dispatched]
        wave_ids = ids[-expected_count:] if expected_count else []
        return reply_fn(scenario_id, wave_ids)

    monkeypatch.setattr(mqs, "collect_replies", _collect)


class TestRunSerialChain:
    def test_wave2_sees_wave1_output(self, task_repo, stub_chain_decompose, monkeypatch):
        scn = f"chain-{uuid.uuid4().hex[:8]}"

        # Simulate execution: each dispatched task succeeds with its goal as output.
        from core.message_queue import ReplyMessage

        def _replies(scenario_id, wave_ids):
            return [ReplyMessage(task_id=tid, success=True,
                                 result={"success": True, "output": f"out-{tid[:8]}"})
                    for tid in wave_ids]
        _stub_collect_replies(monkeypatch, _replies)

        agent = SchedulingAgent()
        agent.initialize({})
        parent = agent.create_task("write and run code")
        result = agent.run(parent, {
            "goal": "write and run code",
            "scenario_id": scn,
            "timeout_seconds": 60,
            "agent_roles": {"execution_agents": [
                {"name": "代码执行专家", "role": "你执行代码"},
            ]},
        })

        assert result["success"] is True
        assert len(result["replies"]) == 2

        # Two dispatch waves => two batches of dispatch rows.
        dispatch_rows = [r for r in MessageRepository().find_by_scenario_id(scn, 50)
                         if r.message_type == "dispatch"]
        assert len(dispatch_rows) == 2

        # The dependent task's dispatch row must carry the predecessor's output.
        by_task = {json.loads(r.content)["task_id"]: json.loads(r.content)
                   for r in dispatch_rows}
        dependent = [c for c in by_task.values()
                     if c["context"].get("upstream_results")]
        assert len(dependent) == 1
        upstream = dependent[0]["context"]["upstream_results"]
        assert len(upstream) == 1
        predecessor_result = next(iter(upstream.values()))
        assert predecessor_result.get("success") is True

    def test_failed_predecessor_skips_dependent(self, task_repo, stub_chain_decompose, monkeypatch):
        """If a predecessor fails, its dependent is skipped (not dispatched)."""
        scn = f"fail-{uuid.uuid4().hex[:8]}"
        from core.message_queue import ReplyMessage

        # Wave 1 (t1) fails.
        def _replies(scenario_id, wave_ids):
            return [ReplyMessage(task_id=tid, success=False,
                                 result={"success": False, "error": "boom", "output": ""})
                    for tid in wave_ids]
        _stub_collect_replies(monkeypatch, _replies)

        agent = SchedulingAgent()
        agent.initialize({})
        parent = agent.create_task("write and run code")
        result = agent.run(parent, {
            "goal": "write and run code",
            "scenario_id": scn,
            "timeout_seconds": 60,
            "agent_roles": {"execution_agents": [
                {"name": "代码执行专家", "role": "你执行代码"},
            ]},
        })

        # t1 failed -> t2 skipped. Only t1 was dispatched.
        dispatch_rows = [r for r in MessageRepository().find_by_scenario_id(scn, 50)
                         if r.message_type == "dispatch"]
        assert len(dispatch_rows) == 1

        # t2 must be in a terminal failed/cancelled state, never running.
        tasks = TaskRepository().find_by_scenario_id(scn)
        t2 = [t for t in tasks if "run code" in (t.goal or "")]
        assert t2, "dependent task t2 not found"
        assert t2[0].state in ("failed", "cancelled"), \
            f"dependent should be skipped, got {t2[0].state}"

