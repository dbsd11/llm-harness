"""
Unit tests for core/ws_server.py (WSDispatcher).

Uses a fake in-memory websocket to avoid a real network round-trip. Covers:
- is_connected reflects the connections map
- defer + _drain_deferred: parked tasks are re-dispatched on reconnect
- _on_task_result: resolves a pending future; late/duplicate result is dropped
- _on_disconnect: fails in-flight futures + marks the server offline
- _on_register: upserts the registry row, acks, drains deferred tasks
"""
import asyncio
import json
import uuid
from datetime import datetime

import pytest

from core.ws_server import WSDispatcher, ConnectionLost
from core.message_queue import TaskMessage
from core import ws_protocol as P
from database.repositories.execution_server_repository import ExecutionServerRepository


class _FakeWS:
    """Minimal async websocket stand-in for websockets.WebSocketServerProtocol."""
    def __init__(self):
        self.sent = []
        self._closed = False

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        self._closed = True


def _make_dispatcher():
    # host/port unused — we never call .run(); we drive handlers directly.
    return WSDispatcher(host="127.0.0.1", port=0)


def _run(coro, loop):
    return loop.run_until_complete(coro)


def _parsed(frame_type, payload, task_id=None):
    """Build a frame and parse it to the dict the handlers expect."""
    return P.parse_frame(P.make_frame(frame_type, payload, task_id=task_id))


# --- is_connected / defer / drain -------------------------------------------

class TestConnectionState:
    def test_is_connected_reflects_connections_map(self):
        d = _make_dispatcher()
        assert d.is_connected("node-1") is False
        d.connections["node-1"] = object()
        assert d.is_connected("node-1") is True


class TestDeferAndDrain:
    def test_defer_parks_then_drain_redispatches(self):
        d = _make_dispatcher()
        msg = TaskMessage("t1", "p", "g", {"server_id": "node-1"})
        d.defer("node-1", "scn", msg)

        assert d.is_connected("node-1") is False
        assert len(d.deferred["node-1"]) == 1

        forwarded = []
        d.schedule_forward = lambda sid, scn, m, did=None: forwarded.append((sid, scn, m))
        d._drain_deferred("node-1")

        assert len(forwarded) == 1
        assert forwarded[0][0] == "node-1"
        assert forwarded[0][2].task_id == "t1"
        # drained list is emptied
        assert d.deferred.get("node-1", []) == []

    def test_drain_with_nothing_parked_is_noop(self):
        d = _make_dispatcher()
        d.schedule_forward = lambda *a: pytest.fail("should not forward")
        d._drain_deferred("node-1")  # no entry -> no error, no forward


# --- task_result correlation -------------------------------------------------

class TestTaskResult:
    def test_resolves_pending_future(self):
        d = _make_dispatcher()
        loop = d.loop
        fut = loop.create_future()
        d.pending["t1"] = fut
        d.task_server["t1"] = "node-1"

        result = {"success": True, "output": "42"}
        d._on_task_result(_parsed(P.TYPE_TASK_RESULT, {
            "task_id": "t1", "success": True, "result": result,
        }, task_id="t1"))

        assert fut.done()
        assert fut.result() == result
        assert "t1" not in d.pending

    def test_late_result_with_no_pending_is_dropped(self):
        d = _make_dispatcher()
        # No pending future for t2 -> must not raise.
        d._on_task_result(_parsed(P.TYPE_TASK_RESULT, {
            "task_id": "t2", "success": True, "result": {},
        }, task_id="t2"))
        assert d.pending == {}

    def test_duplicate_result_does_not_overwrite(self):
        d = _make_dispatcher()
        loop = d.loop
        fut = loop.create_future()
        d.pending["t1"] = fut
        d.task_server["t1"] = "node-1"

        frame = _parsed(P.TYPE_TASK_RESULT, {
            "task_id": "t1", "success": True, "result": {"output": "first"},
        }, task_id="t1")
        d._on_task_result(frame)
        first = fut.result()
        # second arrival -> already done, ignored
        d._on_task_result(frame)
        assert first == {"output": "first"}


# --- disconnect --------------------------------------------------------------

class TestDisconnect:
    def test_fails_inflight_futures_and_marks_offline(self):
        d = _make_dispatcher()
        loop = d.loop
        ws = _FakeWS()
        d.connections["node-1"] = ws
        fut = loop.create_future()
        d.pending["t1"] = fut
        d.task_server["t1"] = "node-1"

        # Registry row must exist for mark_offline to update.
        ExecutionServerRepository().upsert(
            "node-1", name="Node 1", total_quota=4,
            status="running", connected=True,
        )

        _run(d._on_disconnect("node-1", ws), loop)

        assert "node-1" not in d.connections
        assert fut.done()
        assert isinstance(fut.exception(), ConnectionLost)
        row = ExecutionServerRepository().find_by_server_id("node-1")
        assert row.status == "offline"
        assert row.connected in (False, 0)

    def test_disconnect_noop_if_socket_replaced(self):
        # If a duplicate-id registration already replaced the socket, the old
        # socket's disconnect must NOT clear the live connection.
        d = _make_dispatcher()
        old_ws = _FakeWS()
        new_ws = _FakeWS()
        d.connections["node-1"] = new_ws  # replacement happened first

        ExecutionServerRepository().upsert("node-1", name="N", total_quota=1,
                                           connected=True)
        _run(d._on_disconnect("node-1", old_ws), d.loop)

        assert d.connections.get("node-1") is new_ws


# --- register ---------------------------------------------------------------

class TestRegister:
    def test_registers_upserts_ack_and_drains(self):
        d = _make_dispatcher()
        ws = _FakeWS()
        # Park a task before the server connects.
        msg = TaskMessage("t1", "p", "g", {"server_id": "node-1"})
        d.defer("node-1", "scn", msg)

        forwarded = []
        d.schedule_forward = lambda sid, scn, m, did=None: forwarded.append((sid, scn, m))

        frame = _parsed(P.TYPE_REGISTER, {
            "server_id": "node-1", "name": "Node 1",
            "total_quota": 4, "env_info": {"bash": True},
            "source": "execution_server",
        })
        sid = _run(d._on_register(ws, frame), d.loop)

        assert sid == "node-1"
        assert d.connections["node-1"] is ws
        # ack sent
        assert len(ws.sent) == 1
        ack = P.parse_frame(ws.sent[0])
        assert ack["type"] == P.TYPE_ACK
        assert ack["payload"]["ok"] is True
        # registry row
        row = ExecutionServerRepository().find_by_server_id("node-1")
        assert row.name == "Node 1"
        assert row.total_quota == 4
        assert row.connected in (True, 1)
        assert row.source == "execution_server"
        # deferred task drained
        assert len(forwarded) == 1
        assert forwarded[0][2].task_id == "t1"

    def test_register_without_source_defaults_to_execution_server(self):
        """Legacy clients that omit `source` are tagged execution_server."""
        d = _make_dispatcher()
        ws = _FakeWS()
        frame = _parsed(P.TYPE_REGISTER, {
            "server_id": "node-7", "name": "Node 7",
            "total_quota": 1, "env_info": {},
        })
        _run(d._on_register(ws, frame), d.loop)
        row = ExecutionServerRepository().find_by_server_id("node-7")
        assert row.source == "execution_server"

    def test_register_without_server_id_nacks(self):
        d = _make_dispatcher()
        ws = _FakeWS()
        frame = _parsed(P.TYPE_REGISTER, {
            "server_id": "", "name": "x", "total_quota": 1, "env_info": {},
        })
        sid = _run(d._on_register(ws, frame), d.loop)
        assert sid is None
        ack = P.parse_frame(ws.sent[0])
        assert ack["payload"]["ok"] is False
