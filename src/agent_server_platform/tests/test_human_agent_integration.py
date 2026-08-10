# Human Agent — backend protocol integration test
#
# Drives the REAL WSDispatcher code paths the browser JS relies on, using a
# fake in-memory websocket (the repo's established pattern — see
# tests/test_ws_dispatcher.py). Models the browser's full flow:
#
#   register(human-1) -> ack + registry row
#   backend forward_task -> task frame captured on the fake WS
#   browser task_result -> finalize -> task success + reply message
#
# Single-loop driver (no second thread): SQLite uses one shared connection
# across threads, so running forward_task on a separate loop thread would
# deadlock against the main thread's DB ops. In production the WSDispatcher
# runs in its own process with its own ConnectionManager, so this is a
# test-only constraint. Everything here runs on d.loop in the main thread.
import asyncio

import pytest

from core import ws_protocol as P
from core.message_queue import TaskMessage
from core.ws_server import WSDispatcher
from database.models.message import Message
from database.models.task import Task
from database.repositories.base_repository import BaseRepository
from database.repositories.execution_server_repository import ExecutionServerRepository
from datetime import datetime


class _FakeWS:
    """Minimal async websocket stand-in (mirrors test_ws_dispatcher._FakeWS)."""
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        pass


def _parsed(frame_type, payload, task_id=None):
    return P.parse_frame(P.make_frame(frame_type, payload, task_id=task_id))


def _create_task(task_repo, task_id="human-task-1", goal="翻译这段话"):
    task_repo.create(Task(
        task_id=task_id, agent_run_id="ar-1", goal=goal,
        state="pending", priority=0, timeout_seconds=60, max_retries=3,
        retry_count=0, scenario_id=None, context="{}",
        created_at=datetime.now(), updated_at=datetime.now(),
    ))


def test_browser_flow_register_forward_result_finalize(fresh_database, task_repo):
    """The exact contract human_agent.js depends on, end-to-end through WSDispatcher."""
    d = WSDispatcher(host="127.0.0.1", port=0)
    ws = _FakeWS()
    _create_task(task_repo)

    # --- 1. browser sends register (what human_agent.js sendRegister does) ---
    reg_frame = _parsed(P.TYPE_REGISTER, {
        "server_id": "human-1", "name": "human-1",
        "total_quota": 8, "env_info": {"human": True},
        "source": "human_agent",
    })
    d.loop.run_until_complete(d._on_register(ws, reg_frame))

    assert d.is_connected("human-1")
    ack = P.parse_frame(ws.sent[0])
    assert ack["type"] == P.TYPE_ACK and ack["payload"]["ok"] is True
    row = ExecutionServerRepository().find_by_server_id("human-1")
    assert row is not None and row.connected in (True, 1)
    assert row.total_quota == 8
    # source annotation flows through to the registry row (UI uses it to tell
    # human agents apart from execution servers).
    assert row.source == "human_agent"

    # --- 2 + 3. forward -> task frame -> browser submits task_result -> finalize ---
    msg = TaskMessage(
        task_id="human-task-1", parent_task_id="parent-1", goal="翻译这段话",
        context={"role": "译者", "server_id": "human-1"},
    )

    async def driver():
        # forward_task sends the task frame then awaits the result future.
        ft = asyncio.ensure_future(d.forward_task("human-1", None, msg, None))
        # wait until the task frame lands on the fake WS (browser would parse it)
        while len(ws.sent) < 2:
            await asyncio.sleep(0.001)
        # browser submits result (what human_agent.js submit() does) — inner
        # result carries `success`, matching the exec-server task_runner contract.
        d._on_task_result(_parsed(P.TYPE_TASK_RESULT, {
            "task_id": "human-task-1", "success": True,
            "result": {"success": True, "output": "Translate this",
                       "submitted_by": "human-1"},
        }, task_id="human-task-1"))
        await ft  # forward_task resumes -> finalize_task -> terminal

    d.loop.run_until_complete(driver())

    # --- assertions on the task frame the browser received ---
    task_frame = P.parse_frame(ws.sent[1])
    assert task_frame["type"] == P.TYPE_TASK
    assert task_frame["payload"]["task_id"] == "human-task-1"
    assert task_frame["payload"]["goal"] == "翻译这段话"
    assert task_frame["payload"]["context"]["role"] == "译者"
    assert task_frame["payload"]["parent_task_id"] == "parent-1"

    # --- task finalized to success + identity stamped ---
    t = task_repo.find_by_task_id("human-task-1")
    assert t is not None
    assert t.state == "success", f"expected success, got {t.state}"
    assert (t.agent_name or "").endswith("human-1")
    assert t.agent_role == "译者"

    # --- a reply message (execution -> scheduling) was written for collect_replies ---
    replies = BaseRepository(Message).find_by_criteria({"task_id": "human-task-1"})
    assert any(m.message_type == "reply" for m in replies)
    # future consumed, no leak
    assert "human-task-1" not in d.pending
