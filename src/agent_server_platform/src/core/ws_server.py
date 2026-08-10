# WSDispatcher - backend WebSocket server for execution-agent-servers.
#
# Accepts inbound WS connections from remote exec-servers, tracks a live
# registry (mirrored to the execution_servers table), and bridges the
# CentralDispatcher's forwards to the right server connection.
import os
import time
import asyncio
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import json

import websockets
from websockets.exceptions import ConnectionClosed

from logger import logger
from core import ws_protocol as P
from core.ws_protocol import (
    parse_frame, ack_frame, task_frame,
    TYPE_REGISTER, TYPE_STATUS, TYPE_TASK_EVENT, TYPE_TASK_RESULT,
    EVENT_AGENT_CREATED, EVENT_TASK_STARTED,
    STATUS_IDLE, STATUS_OFFLINE,
)
from core.message_queue import TaskMessage
from core.central_dispatcher import finalize_task, central_dispatcher
from core.event_bus import event_bus
from core.state_machine import TASK_EXECUTION_COMPLETE_STATES
from database.repositories.execution_server_repository import ExecutionServerRepository
from database.repositories.task_repository import TaskRepository


class ConnectionLost(Exception):
    """Raised on a pending forward future when the target server disconnects."""


class WSDispatcher:
    """Backend WS server + execution-server registry."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        from common.utils.global_loop_util import get_random_work_loop
        self.loop = get_random_work_loop()
        # server_id -> websocket
        self.connections: Dict[str, Any] = {}
        # task_id -> asyncio.Future (awaited by forward_task)
        self.pending: Dict[str, Any] = {}
        # task_id -> server_id (so disconnect can fail the right futures)
        self.task_server: Dict[str, str] = {}
        # server_id -> list[(scenario_id, TaskMessage, dispatch_id)] parked until reconnect
        self.deferred: Dict[str, list] = {}
        self._lock = threading.Lock()
        self._server_repo = ExecutionServerRepository()

    # --- lifecycle ----------------------------------------------------------

    def run(self) -> None:
        """Run the WS server forever (blocks the calling thread).

        Schedules _heartbeat_sweeper + _main on the shared worker loop from
        global_loop_util (already running in a daemon thread), then blocks the
        calling thread.
        """
        asyncio.run_coroutine_threadsafe(self._heartbeat_sweeper(), self.loop)
        main_fut = asyncio.run_coroutine_threadsafe(self._main(), self.loop)
        main_fut.result()  # blocks until _main returns (process shutdown)

    async def _main(self) -> None:
        async with websockets.serve(self._handler, self.host, self.port):
            logger.info(f"WSDispatcher listening on ws://{self.host}:{self.port}")
            await asyncio.Future()  # run forever

    # --- public API (called from the CentralDispatcher thread) --------------

    def is_connected(self, server_id: str) -> bool:
        with self._lock:
            return server_id in self.connections

    def schedule_forward(self, server_id: str, scenario_id: Optional[str],
                         msg: TaskMessage, dispatch_id: Optional[int] = None) -> None:
        """Schedule a forward on the WS loop (fire-and-forget)."""
        asyncio.run_coroutine_threadsafe(
            self.forward_task(server_id, scenario_id, msg, dispatch_id), self.loop
        )

    def defer(self, server_id: str, scenario_id: Optional[str],
              msg: TaskMessage, dispatch_id: Optional[int] = None) -> None:
        """Park a task until the server reconnects. Does NOT ack the dispatch
        (the task isn't done); stays in-flight so the consumer won't redeliver
        within this process. Re-forwarded on reconnect, then acked on finalize.
        """
        with self._lock:
            self.deferred.setdefault(server_id, []).append((scenario_id, msg, dispatch_id))
            n = len(self.deferred[server_id])
        logger.info(f"Deferred task {msg.task_id} for server {server_id} "
                    f"(parked={n})")

    # --- forward + reply correlation (runs on the WS loop) ------------------

    async def forward_task(self, server_id: str, scenario_id: Optional[str],
                           msg: TaskMessage,
                           dispatch_id: Optional[int] = None) -> None:
        """Send a task to a server and await its result.

        Owns the full WS-path lifecycle: mark started -> send -> await result ->
        finalize -> ack. On any connection failure, defer (no ack) for
        re-dispatch on reconnect.
        """
        task_repo = TaskRepository()

        # Idempotency: redelivery of an already-terminal task -> ack + skip.
        existing = task_repo.find_by_task_id(msg.task_id)
        if existing and existing.state in TASK_EXECUTION_COMPLETE_STATES:
            logger.info(f"forward_task: task {msg.task_id} already terminal "
                        f"({existing.state}); skipping redelivery")
            central_dispatcher._ack(dispatch_id, msg.task_id)
            return

        future = self.loop.create_future()
        with self._lock:
            self.pending[msg.task_id] = future
            self.task_server[msg.task_id] = server_id
            ws = self.connections.get(server_id)

        if ws is None:
            # disconnected between schedule_forward and run -> park
            with self._lock:
                self.pending.pop(msg.task_id, None)
                self.task_server.pop(msg.task_id, None)
            self.defer(server_id, scenario_id, msg, dispatch_id)
            return

        try:
            task_repo.mark_as_started(msg.task_id)
            await ws.send(task_frame(
                msg.task_id, msg.parent_task_id, msg.goal, msg.context
            ))
            logger.info(f"Forwarded task {msg.task_id} to server {server_id}")
            start_time = time.time()
            result = await future  # resolved by _on_task_result or failed on disconnect
        except Exception as e:
            logger.warning(f"forward_task: task {msg.task_id} to {server_id} "
                           f"failed: {e}; deferring for reconnect")
            with self._lock:
                self.pending.pop(msg.task_id, None)
                self.task_server.pop(msg.task_id, None)
            self.defer(server_id, scenario_id, msg, dispatch_id)
            return

        with self._lock:
            self.pending.pop(msg.task_id, None)
            self.task_server.pop(msg.task_id, None)
        elapsed = time.time() - start_time
        # The remote exec-server is the "agent" here; record its id + role
        # + duration so the task monitor shows them like the local path.
        agent_role = (result or {}).get("role") or msg.context.get("role")
        finalize_task(scenario_id, msg.task_id, result,
                      agent_name=f"ExecutionServer:{server_id}",
                      agent_role=agent_role,
                      execution_duration=round(elapsed, 3))
        # Ack the dispatch now that the task is finalized (ack-after-execute).
        central_dispatcher._ack(dispatch_id, msg.task_id)

    # --- connection handler -------------------------------------------------

    async def _handler(self, websocket) -> None:
        server_id: Optional[str] = None
        try:
            async for raw in websocket:
                try:
                    frame = parse_frame(raw)
                except ValueError as e:
                    logger.warning(f"Bad frame: {e}")
                    continue
                t = frame["type"]
                if t == TYPE_REGISTER:
                    server_id = await self._on_register(websocket, frame)
                elif t == TYPE_STATUS:
                    self._on_status(server_id, frame)
                elif t == TYPE_TASK_EVENT:
                    self._on_task_event(server_id, frame)
                elif t == TYPE_TASK_RESULT:
                    self._on_task_result(frame)
                else:
                    logger.warning(f"Unknown frame type: {t}")
        except ConnectionClosed:
            pass
        except Exception as e:
            logger.error(f"WS handler error: {e}")
        finally:
            if server_id:
                await self._on_disconnect(server_id, websocket)

    async def _on_register(self, websocket, frame) -> Optional[str]:
        p = frame["payload"]
        sid = p.get("server_id")
        if not sid:
            await websocket.send(ack_frame(False, error="missing server_id"))
            return None
        with self._lock:
            old = self.connections.get(sid)
            if old is not None and old is not websocket:
                # ponytail: duplicate server_id -> kick the old socket; its
                # _on_disconnect no-ops because connections[sid] no longer is it.
                asyncio.ensure_future(old.close())
            self.connections[sid] = websocket
        self._server_repo.upsert(
            server_id=sid, name=p.get("name", sid),
            total_quota=p.get("total_quota", 0),
            env_info=p.get("env_info", {}),
            status=STATUS_IDLE, connected=True,
            last_heartbeat=datetime.now(),
            source=p.get("source") or "execution_server",
        )
        await websocket.send(ack_frame(True))
        logger.info(f"Execution server registered: {sid} "
                    f"(quota={p.get('total_quota')})")
        # drain tasks parked while this server was offline
        self._drain_deferred(sid)
        return sid

    def _on_status(self, server_id: Optional[str], frame) -> None:
        if not server_id:
            return
        p = frame["payload"]
        self._server_repo.update_status(
            server_id,
            status=p.get("status", STATUS_IDLE),
            running_count=p.get("running_count", 0),
            connected=True,
            env_info=p.get("env_info"),
            last_heartbeat=datetime.now(),
        )

    def _on_task_event(self, server_id: Optional[str], frame) -> None:
        p = frame["payload"]
        event = p.get("event")
        tid = frame.get("task_id")
        if event == EVENT_AGENT_CREATED:
            event_bus.emit("task.execution_agent_created", {
                "task_id": tid, "role": p.get("role"), "server_id": server_id,
            })
        elif event == EVENT_TASK_STARTED:
            event_bus.emit("task.execution_started", {
                "task_id": tid, "server_id": server_id,
            })
        else:
            logger.debug(f"task_event {event} for {tid}")

    def _on_task_result(self, frame) -> None:
        p = frame["payload"]
        tid = p.get("task_id") or frame.get("task_id")
        result = p.get("result", {})
        with self._lock:
            fut = self.pending.get(tid)
            self.task_server.pop(tid, None)
        if fut is None:
            # ponytail: late result for a task already re-deferred/re-run after
            # a disconnect -> drop. Narrow dup window; documented in the plan.
            logger.warning(f"task_result for {tid} with no pending future; dropping")
            return
        if not fut.done():
            fut.set_result(result)
            with self._lock:
                # Resolved -> no longer awaiting; forward_task holds its own
                # reference and its later pop is idempotent.
                self.pending.pop(tid, None)

    async def _on_disconnect(self, server_id: str, websocket) -> None:
        with self._lock:
            # Only clear if this socket still owns the server_id slot
            # (a duplicate-id replacement may have already taken it over).
            if self.connections.get(server_id) is not websocket:
                return
            del self.connections[server_id]
            # Fail in-flight futures for this server -> forward_task will defer.
            to_fail = [tid for tid, s in self.task_server.items()
                       if s == server_id]
            for tid in to_fail:
                fut = self.pending.get(tid)
                if fut and not fut.done():
                    fut.set_exception(ConnectionLost(server_id))
        self._server_repo.mark_offline(server_id)
        logger.info(f"Execution server disconnected: {server_id} "
                    f"(failing {len(to_fail)} in-flight task(s))")

    def _drain_deferred(self, server_id: str) -> None:
        with self._lock:
            parked = self.deferred.pop(server_id, [])
        for scenario_id, msg, dispatch_id in parked:
            logger.info(f"Re-dispatching deferred task {msg.task_id} "
                        f"to {server_id}")
            self.schedule_forward(server_id, scenario_id, msg, dispatch_id)

    # --- heartbeat sweeper --------------------------------------------------

    async def _heartbeat_sweeper(self) -> None:
        interval = int(os.getenv("HEARTBEAT_INTERVAL", "5"))
        threshold = max(interval * 3, 10)
        while True:
            await asyncio.sleep(interval)
            try:
                stale_before = datetime.now() - timedelta(seconds=threshold)
                n = self._server_repo.mark_stale_offline(stale_before)
                if n:
                    logger.info(f"Heartbeat sweeper marked {n} stale server(s) offline")
            except Exception as e:
                logger.error(f"Heartbeat sweeper error: {e}")
