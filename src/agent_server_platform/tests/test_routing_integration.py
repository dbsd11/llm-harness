"""
Integration tests for execution-server routing:

- SchedulingAgent.run stamps server_id onto dispatched subtask contexts when
  the matched role has a server configured (and leaves it absent otherwise).
- AgentManager injects agent_roles from the scenario config into the
  SchedulingAgent's run context, so routing can resolve even when the scenario
  didn't pass agent_roles explicitly.
"""
import json
import uuid
from datetime import datetime

import pytest

from core.agents.scheduling_agent import SchedulingAgent
from core.state_machine import TaskState
from database.repositories.message_repository import MessageRepository
from database.repositories.scenario_repository import ScenarioRepository
from database.models.scenario import Scenario


# --- helpers ----------------------------------------------------------------

def _role_subtasks(goal, role):
    """LLM-style subtask list: one subtask with a role in its context."""
    return [{
        "goal": f"answer {goal}",
        "type": "execution",
        "priority": 0,
        "timeout_seconds": 60,
        "context": {"role": role, "system_prompt": "s", "question": goal},
    }]


@pytest.fixture
def stub_decompose(monkeypatch):
    """Make _decompose_goal deterministic (no real LLM)."""
    from core import llm_client
    monkeypatch.setattr(llm_client.llm_client, "client", object())  # truthy

    def _decompose(goal, context):
        # Use the role from agent_roles if present, else a default.
        roles = (context.get("agent_roles") or {}).get("execution_agents", [])
        role = roles[0]["name"] if roles else "数学家"
        return _role_subtasks(goal, role)
    monkeypatch.setattr(llm_client.llm_client, "decompose_goal", _decompose)
    return llm_client.llm_client


def _dispatch_rows(scenario_id):
    rows = MessageRepository().find_by_scenario_id(scenario_id, limit=50)
    return [r for r in rows if r.message_type == "dispatch"]


# --- SchedulingAgent stamps server_id --------------------------------------

class TestServerIdStamping:
    def test_role_with_server_is_stamped_on_dispatch(self, task_repo, stub_decompose):
        scn = f"stamp-{uuid.uuid4().hex[:8]}"
        agent = SchedulingAgent()
        agent.initialize({})
        parent = agent.create_task("compute 2+2")

        result = agent.run(parent, {
            "goal": "compute 2+2",
            "scenario_id": scn,
            "timeout_seconds": 2,
            "agent_roles": {"execution_agents": [
                {"name": "数学家", "role": "r", "server_id": "node-1"},
            ]},
        })

        assert result["success"] is True
        rows = _dispatch_rows(scn)
        assert len(rows) == 1
        content = json.loads(rows[0].content)
        assert content["context"].get("server_id") == "node-1"

    def test_role_without_server_has_no_server_id(self, task_repo, stub_decompose):
        scn = f"nostamp-{uuid.uuid4().hex[:8]}"
        agent = SchedulingAgent()
        agent.initialize({})
        parent = agent.create_task("compute 2+2")

        result = agent.run(parent, {
            "goal": "compute 2+2",
            "scenario_id": scn,
            "timeout_seconds": 2,
            "agent_roles": {"execution_agents": [
                {"name": "数学家", "role": "r"},  # no server_id -> local
            ]},
        })

        assert result["success"] is True
        rows = _dispatch_rows(scn)
        assert len(rows) == 1
        assert "server_id" not in json.loads(rows[0].content)["context"]


# --- AgentManager injects agent_roles --------------------------------------

class TestAgentRolesInjection:
    def test_agent_roles_loaded_from_scenario_config(self, task_repo, monkeypatch):
        from agents.agent_manager import agent_manager
        from core.agents import scheduling_agent as sa_mod

        scn_id = f"inj-{uuid.uuid4().hex[:8]}"
        agent_roles = {"execution_agents": [
            {"name": "数学家", "role": "r", "server_id": "node-1"},
        ]}
        # Persist a scenario whose config carries agent_roles.
        ScenarioRepository().create(Scenario(
            scenario_id=scn_id, scenario_type="simple_qa",
            name="t", description="", state="initializing",
            config=json.dumps({"agent_roles": agent_roles, "question": "?"}),
            context=json.dumps({"trace_id": "x"}),
            created_at=datetime.now(), updated_at=datetime.now(),
        ))

        captured = {}

        # Replace SchedulingAgent.run to capture the context it receives.
        orig_run = sa_mod.SchedulingAgent.run

        def _capture(self, task_id, context):
            captured["agent_roles"] = context.get("agent_roles")
            return {"success": True, "topic_id": "t", "subtask_ids": [],
                    "subtask_count": 0}

        monkeypatch.setattr(sa_mod.SchedulingAgent, "run", _capture)
        task_id = agent_manager.submit_task(
            goal="compute 2+2", agent_type="scheduling",
            scenario_id=scn_id, timeout_seconds=10,
        )
        # Wait for the async work loop to fully finish this task (terminal
        # state) BEFORE teardown closes the DB — otherwise the worker thread
        # writes to a closed connection and segfaults.
        import time
        deadline = time.time() + 5
        while time.time() < deadline:
            t = task_repo.find_by_task_id(task_id)
            if t and t.state in ("success", "failed", "timeout", "cancelled"):
                break
            time.sleep(0.05)
        monkeypatch.setattr(sa_mod.SchedulingAgent, "run", orig_run)

        # agent_roles was injected from the scenario config (not passed in
        # submit_task, so it had to come from the DB load).
        assert captured.get("agent_roles") == agent_roles
