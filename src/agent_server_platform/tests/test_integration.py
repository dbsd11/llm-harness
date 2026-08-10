"""
Integration tests for end-to-end system flows.

Tests full lifecycle scenarios:
- SchedulingAgent creates subtasks and dispatches via MQS
- ExecutionAgent runs scripts/commands through sandbox
- ScenarioManager manages scenario lifecycle
- AgentManager submits tasks and tracks runs
- Event flow through the system
- Topic completion detection
"""
import pytest
import time
import uuid
import json
from datetime import datetime

from core.state_machine import TaskState, ScenarioState
from core.event_bus import event_bus
from core.agents.scheduling_agent import SchedulingAgent
from core.agents.execution_agent import ExecutionAgent
from core.agents.base_agent import AgentRun
from agents.agent_manager import AgentManager
from scenarios.scenario_manager import ScenarioManager
from scenarios.base_scenario import BaseScenario, ScenarioContext, AgentRole
from database.repositories.task_repository import TaskRepository
from database.repositories.agent_repository import AgentRepository
from database.repositories.scenario_repository import ScenarioRepository
from database.models.task import Task


@pytest.fixture
def stub_llm(monkeypatch):
    """Stub the shared LLM so ExecutionAgent tests don't hit the network."""
    from core import llm_client
    monkeypatch.setattr(llm_client.llm_client, "client", object())  # truthy
    monkeypatch.setattr(
        llm_client.llm_client, "chat",
        lambda messages, temperature=0.7: "42",
    )
    return llm_client.llm_client


class TestSchedulingAgentIntegration:
    """Test SchedulingAgent creating tasks and dispatching via MQS."""

    def test_run_creates_subtasks_in_db(self, task_repo, stub_llm):
        agent = SchedulingAgent()
        agent.initialize({})

        parent_id = agent.create_task("Analyze data and generate report")
        result = agent.run(parent_id, {
            "goal": "Analyze data and generate report"
        })

        assert result["success"] is True
        assert result["subtask_count"] == 2
        assert result["topic_id"] is not None

        # Verify subtasks exist in DB
        subtasks = task_repo.find_by_parent_task_id(parent_id)
        assert len(subtasks) == 2
        for sub in subtasks:
            assert sub.topic_id == result["topic_id"]
            assert sub.idempotency_key is not None

    def test_run_with_scenario_dispatches_via_mqs(self, task_repo, stub_llm):
        """When scenario_id is provided, subtasks are dispatched to the MQ.

        The CentralDispatcher (which consumes + executes) isn't running in unit
        tests, so we verify dispatch rows were written and collect_replies
        returns promptly with a short timeout.
        """
        from core.message_queue import mqs
        from database.repositories.message_repository import MessageRepository

        scenario_id = f"int-scenario-{uuid.uuid4().hex[:8]}"

        agent = SchedulingAgent()
        agent.initialize({})

        parent_id = agent.create_task("Echo hello and echo world")

        result = agent.run(parent_id, {
            "goal": "Echo hello and echo world",
            "scenario_id": scenario_id,
            "timeout_seconds": 2,  # short collect timeout (no consumer running)
        })

        assert result["success"] is True
        assert result["subtask_count"] == 2
        assert "replies" in result

        # Dispatch rows were written for both subtasks
        rows = MessageRepository().find_by_scenario_id(scenario_id, limit=50)
        dispatch_rows = [r for r in rows if r.message_type == "dispatch"]
        assert len(dispatch_rows) == 2


class TestExecutionAgentIntegration:
    """Test ExecutionAgent running through the full RECEIVE→PROCESS→RESPOND flow."""

    def test_full_llm_execution_flow(self, event_bus, stub_llm):
        received = []
        handler = lambda e: received.append(e)
        event_bus.subscribe("task.execution_started", handler)
        event_bus.subscribe("task.execution_completed", handler)

        agent = ExecutionAgent()
        agent.initialize({"role": "数学家", "system_prompt": "math expert"})
        try:
            result = agent.run("exec-test-1", {"goal": "What is 2+2?"})

            assert result["success"] is True
            assert result["output"] == "42"
        finally:
            agent.cleanup()

        time.sleep(0.3)
        event_bus.unsubscribe("task.execution_started", handler)
        event_bus.unsubscribe("task.execution_completed", handler)

        types = {e["event_type"] for e in received}
        assert "task.execution_started" in types
        assert "task.execution_completed" in types


class TestAgentRunIntegration:
    """Test AgentRun with real agents."""

    def test_agent_run_lifecycle(self, stub_llm):
        agent = ExecutionAgent()
        agent.initialize({"role": "数学家", "system_prompt": "s"})

        run = AgentRun(agent, "run-test-1", {"goal": "What is 2+2?"})

        assert run.processing is False
        run.start()
        assert run.processing is True

        result = agent.run("run-test-1", {"goal": "What is 2+2?"})
        run.complete(result)

        assert run.processing is False
        assert run.result["success"] is True

        d = run.to_dict()
        assert d["task_id"] == "run-test-1"
        assert d["processing"] is False
        assert d["result"]["success"] is True

        agent.cleanup()

    def test_agent_run_failure_lifecycle(self):
        agent = ExecutionAgent()
        agent.initialize({})

        run = AgentRun(agent, "run-fail-1", {})
        run.start()
        run.fail("Intentional failure")

        assert run.processing is False
        assert run.error == "Intentional failure"
        assert run.completed_at is not None

        agent.cleanup()


class TestScenarioLifecycle:
    """Test scenario create → start → complete lifecycle."""

    def test_scenario_context_lifecycle(self):
        from scenarios.base_scenario import ScenarioState as BaseScenarioState

        ctx = ScenarioContext("test-scenario-1", {"key": "value"})
        assert ctx.state == BaseScenarioState.INITIALIZING
        assert ctx.scenario_id == "test-scenario-1"

        d = ctx.to_dict()
        assert d["state"] == "initializing"
        assert d["config"] == {"key": "value"}
        assert d["error"] is None


class TestAgentRoleDefinition:
    """Test agent role definition for scenarios (角色定义)."""

    def test_agent_role_creation(self):
        role = AgentRole("scheduler-1", "scheduling",
                         responsibilities=["goal decomposition", "task dispatch"])
        assert role.role_id == "scheduler-1"
        assert role.agent_type == "scheduling"
        assert "goal decomposition" in role.responsibilities

    def test_agent_role_to_dict(self):
        role = AgentRole("exec-1", "execution",
                         responsibilities=["run scripts"],
                         config={"sandbox": True})
        d = role.to_dict()
        assert d["role_id"] == "exec-1"
        assert d["agent_type"] == "execution"
        assert d["responsibilities"] == ["run scripts"]
        assert d["config"] == {"sandbox": True}


class TestTopicCompletion:
    """Test topic completion tracking (子主题结束)."""

    def test_topic_completion_detection(self, task_repo, event_bus):
        """When all subtasks in a topic are terminal, topic.completed fires."""
        received = []
        handler = lambda e: received.append(e)
        event_bus.subscribe("topic.completed", handler)

        topic_id = f"topic-{uuid.uuid4().hex[:8]}"
        parent_id = f"parent-{uuid.uuid4().hex[:8]}"

        # Create sibling tasks in the same topic
        for i in range(3):
            tid = f"sub-{i}-{uuid.uuid4().hex[:8]}"
            task_repo.create(Task(
                task_id=tid,
                parent_task_id=parent_id,
                topic_id=topic_id,
                goal=f"Subtask {i}",
                state=TaskState.SUCCESS.value,  # all completed
                created_at=datetime.now(),
                updated_at=datetime.now(),
            ))

        # Manually trigger topic completion check
        from agents.agent_manager import AgentManager
        mgr = AgentManager()
        mgr._check_topic_completion(f"sub-0-{uuid.uuid4().hex[:8]}")

        # The check uses find_by_task_id which may not find our fake ID,
        # so let's use a real task_id from the topic
        subtasks = task_repo.find_by_topic_id(topic_id)
        mgr._check_topic_completion(subtasks[0].task_id)

        time.sleep(0.5)
        event_bus.unsubscribe("topic.completed", handler)

        completed = [e for e in received
                     if e["event_type"] == "topic.completed"]
        assert len(completed) >= 1
        assert completed[0]["data"]["topic_id"] == topic_id

        mgr.shutdown()

    def test_partial_topic_no_completion(self, task_repo, event_bus):
        """When some subtasks are still running, topic.completed should NOT fire."""
        received = []
        handler = lambda e: received.append(e)
        event_bus.subscribe("topic.completed", handler)

        topic_id = f"partial-{uuid.uuid4().hex[:8]}"
        parent_id = f"parent-{uuid.uuid4().hex[:8]}"

        # One success, one still running
        task_repo.create(Task(
            task_id=f"done-{uuid.uuid4().hex[:8]}",
            parent_task_id=parent_id,
            topic_id=topic_id,
            goal="Done",
            state=TaskState.SUCCESS.value,
            created_at=datetime.now(),
            updated_at=datetime.now(),
        ))
        running_task_id = f"running-{uuid.uuid4().hex[:8]}"
        task_repo.create(Task(
            task_id=running_task_id,
            parent_task_id=parent_id,
            topic_id=topic_id,
            goal="Still running",
            state=TaskState.RUNNING.value,
            created_at=datetime.now(),
            updated_at=datetime.now(),
        ))

        from agents.agent_manager import AgentManager
        mgr = AgentManager()
        mgr._check_topic_completion(running_task_id)

        time.sleep(0.5)
        event_bus.unsubscribe("topic.completed", handler)

        completed = [e for e in received
                     if e["event_type"] == "topic.completed"]
        assert len(completed) == 0

        mgr.shutdown()


class TestEventTraceContext:
    """Test trace_id linking related events (埋点)."""

    def test_events_linked_by_trace_id(self, event_bus, event_repo):
        trace_id = event_bus.emit("test.trace.1", {"step": "start"})
        event_bus.emit("test.trace.2", {"step": "end"}, trace_id=trace_id)

        time.sleep(0.5)

        events = event_repo.find_by_trace_id(trace_id)
        assert len(events) >= 2
        event_types = {e.event_type for e in events}
        assert "test.trace.1" in event_types
        assert "test.trace.2" in event_types
