"""
Unit tests for the slimmed core/message_queue.py.

The queue is now a pure transport: dispatch_subtasks writes dispatch rows,
collect_replies reads reply rows. Execution is handled by the
CentralDispatcher (tested separately), so these tests write reply rows
manually to verify the round-trip.
"""
import pytest
import uuid
import json
from datetime import datetime

from core.message_queue import mqs, TaskMessage, ReplyMessage
from database.repositories.message_repository import MessageRepository
from database.models.message import Message


class TestTaskMessage:
    def test_creation(self):
        msg = TaskMessage("task-1", "parent-1", "Analyze data",
                          context={"key": "value"})
        assert msg.task_id == "task-1"
        assert msg.parent_task_id == "parent-1"
        assert msg.goal == "Analyze data"
        assert msg.context == {"key": "value"}

    def test_default_context(self):
        msg = TaskMessage("task-1", "parent-1", "goal")
        assert msg.context == {}


class TestReplyMessage:
    def test_creation(self):
        msg = ReplyMessage("task-1", True, {"output": "done"})
        assert msg.task_id == "task-1"
        assert msg.success is True
        assert msg.result == {"output": "done"}

    def test_default_result(self):
        msg = ReplyMessage("task-1", False)
        assert msg.result == {}


def _write_reply(scenario_id, task_id, success=True, result=None):
    """Simulate the CentralDispatcher writing a reply row."""
    MessageRepository().create(Message(
        scenario_id=scenario_id,
        task_id=task_id,
        from_agent="execution",
        to_agent="scheduling",
        message_type="reply",
        content=json.dumps({
            "task_id": task_id,
            "success": success,
            "result": result or {"output": "done"},
        }, ensure_ascii=False),
        timestamp=datetime.now(),
    ))


class TestMessageQueueService:
    def test_dispatch_writes_dispatch_rows(self):
        scenario_id = f"mqs-{uuid.uuid4().hex[:8]}"
        mqs.dispatch_subtasks(scenario_id, [
            TaskMessage(task_id="t1", parent_task_id="p", goal="g1",
                        context={"role": "math", "server_id": "node-1"}),
            TaskMessage(task_id="t2", parent_task_id="p", goal="g2",
                        context={"role": "math"}),
        ])

        rows = MessageRepository().find_by_scenario_id(scenario_id, limit=50)
        dispatch_rows = [r for r in rows if r.message_type == "dispatch"]
        assert len(dispatch_rows) == 2
        # server_id is carried through in the context payload (match by task_id;
        # row order is timestamp-desc and near-simultaneous)
        by_task = {json.loads(r.content)["task_id"]: json.loads(r.content)
                   for r in dispatch_rows}
        assert by_task["t1"]["context"].get("server_id") == "node-1"
        assert "server_id" not in by_task["t2"]["context"]

    def test_collect_replies_roundtrip(self):
        scenario_id = f"rt-{uuid.uuid4().hex[:8]}"
        mqs.dispatch_subtasks(scenario_id, [
            TaskMessage(task_id="t1", parent_task_id="", goal="g1", context={}),
        ])
        _write_reply(scenario_id, "t1", True, {"output": "42"})

        replies = mqs.collect_replies(scenario_id, expected_count=1, timeout=5)
        assert len(replies) == 1
        assert replies[0].task_id == "t1"
        assert replies[0].success is True
        assert replies[0].result == {"output": "42"}

    def test_collect_timeout_returns_partial(self):
        scenario_id = f"to-{uuid.uuid4().hex[:8]}"
        # No reply rows written -> collect times out with whatever it has.
        replies = mqs.collect_replies(scenario_id, expected_count=2, timeout=1)
        assert replies == []

    def test_scenario_isolation(self):
        s1 = f"iso1-{uuid.uuid4().hex[:8]}"
        s2 = f"iso2-{uuid.uuid4().hex[:8]}"
        _write_reply(s1, "t1", True, {"output": "s1"})
        _write_reply(s2, "t2", True, {"output": "s2"})

        r1 = mqs.collect_replies(s1, 1, timeout=5)
        r2 = mqs.collect_replies(s2, 1, timeout=5)
        assert len(r1) == 1 and r1[0].task_id == "t1"
        assert len(r2) == 1 and r2[0].task_id == "t2"
