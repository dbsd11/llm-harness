"""WebSocket Server — aiohttp single-port WS + REST API.

Accepts WS connections from execution-agent-servers and human agents,
maintains a live registry (mirrored to execution_servers DB), and bridges
CentralDispatcher task forwards to the right server connection.

Frame protocol aligned with core/ws_protocol.py:
  register     -> {server_id, name, total_quota, env_info, source}
  status       -> {status, total_quota, running_count, env_info}
  task_result  -> {task_id, success, result}
  task_event   -> {event, role?, ...}
  ACK          -> {ok: bool}
  TASK         -> {task_id, parent_task_id, goal, context}
"""
import asyncio
import json
import logging
import os
import ssl
import threading
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional, Dict, Any

from aiohttp import web, WSMsgType

from logger import logger
from database.repositories.execution_server_repository import ExecutionServerRepository
from database.repositories.message_repository import MessageRepository
from database.repositories.task_repository import TaskRepository
from database import init_database
from .config import load_config
from .api import register_routes

from core import ws_protocol as P
from core.ws_protocol import (
    parse_frame, make_frame, ack_frame, task_frame,
    TYPE_REGISTER, TYPE_STATUS, TYPE_TASK_EVENT, TYPE_TASK_RESULT, TYPE_ACK, TYPE_EVENT,
    EVENT_AGENT_CREATED, EVENT_TASK_STARTED,
    STATUS_IDLE, STATUS_OFFLINE,
)
from core.central_dispatcher import finalize_task, central_dispatcher
from core.event_bus import event_bus
from core.message_queue import TaskMessage
from core.state_machine import TASK_EXECUTION_COMPLETE_STATES

# 专用 DB 线程池（见 common.utils.global_loop_util）：把同步 DB I/O 移出 aiohttp
# event loop，避免心跳 / 任务结果 / 分发等高频路径阻塞整个 WS 服务。
from common.utils.global_loop_util import run_in_db_thread

# 单任务转发结果等待上限（秒）：超时则按连接异常处理（defer，不 ack），避免
# agent 心跳仍在但永不回结果导致 forward_task 永久 await（夯死协程）。
_TASK_FORWARD_TIMEOUT = int(os.getenv("TASK_FORWARD_TIMEOUT", "1800"))
# 单订阅者单条事件推送发送超时（秒）：超时即视为慢消费者，跳过本条并累计失败。
_BROADCAST_SEND_TIMEOUT = float(os.getenv("BROADCAST_SEND_TIMEOUT", "2"))
# 广播队列上限：溢出丢弃新事件（事件流尽力推送，面向 UI，可丢）。
_BROADCAST_QUEUE_MAXSIZE = int(os.getenv("BROADCAST_QUEUE_MAXSIZE", "500"))
# 订阅者连续推送失败 N 次后剔除，防止死连接拖慢广播 worker。
_BROADCAST_FAIL_THRESHOLD = int(os.getenv("BROADCAST_FAIL_THRESHOLD", "3"))


class ConnectionLost(Exception):
    """Raised on a pending forward future when the target server disconnects."""


@dataclass
class ConnectedServer:
    """已连接的执行服务器信息"""
    ws: web.WebSocketResponse
    server_id: str
    connected_at: datetime
    last_heartbeat: datetime
    tenant_id: str = None  # 租户 ID（来自 API Key）


class WebSocketServer:
    """Unified WS + HTTP server managing execution-server connections and task dispatch."""

    def __init__(self, host: str, port: int, heartbeat_timeout: int = 15,
                 ssl_cert: Optional[str] = None, ssl_key: Optional[str] = None):
        self.host = host
        self.port = port
        self.heartbeat_timeout = heartbeat_timeout
        # TLS：设置证书+私钥后端口以 wss/https 提供服务（单端口，与明文 ws/http 互斥）
        self.ssl_cert = ssl_cert
        self.ssl_key = ssl_key
        # server_id -> ConnectedServer
        self.connections: Dict[str, ConnectedServer] = {}
        # /subscribe endpoint subscribers: ws -> tenant_id
        self.subscribers: Dict[web.WebSocketResponse, str] = {}
        # 广播 fan-out 队列 + 慢订阅者连续失败计数（队列在 run() 中于 event loop 上创建）
        self._broadcast_queue: Optional[asyncio.Queue] = None
        self._broadcast_fail: Dict[web.WebSocketResponse, int] = {}
        # forward_task correlation: task_id -> asyncio.Future
        self.pending: Dict[str, Any] = {}
        # task_id -> server_id (reverse index for disconnect cleanup)
        self.task_server: Dict[str, str] = {}
        # server_id -> parked tasks [(scenario_id, TaskMessage, dispatch_id), ...]
        self.deferred: Dict[str, list] = {}
        # thread-safety for CentralDispatcher cross-thread access
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.server_repo = ExecutionServerRepository()
        self.message_repo = MessageRepository()
        self.app = self._make_app()

    def _hook_event_bus(self) -> None:
        """Subscribe to all event_bus events and broadcast to WS subscribers."""
        from core.event_bus import event_bus

        def _forward_to_ws(event: dict):
            """Forward event to WebSocket subscribers via the event loop."""
            loop = self._loop
            if not loop or loop.is_closed():
                return
            # 提取 tenant_id（如果存在）
            event_data = event.get("data", {})
            tenant_id = event_data.get("tenant_id") if isinstance(event_data, dict) else None
            asyncio.run_coroutine_threadsafe(
                self._broadcast_event(event["event_type"], {
                    "trace_id": event.get("trace_id"),
                    "data": event.get("data"),
                    "metadata": event.get("metadata"),
                    "timestamp": event.get("timestamp"),
                    "tenant_id": tenant_id,  # 租户隔离
                }),
                loop,
            )

        # Subscribe to wildcard patterns for all major event families
        for prefix in ("scenario", "task", "topic", "recovery"):
            event_bus.subscribe(f"{prefix}.*", _forward_to_ws)
        logger.info("EventBus -> WS broadcast hook installed")

    def is_connected(self, server_id: str) -> bool:
        """Thread-safe: check whether a server is currently connected."""
        with self._lock:
            return server_id in self.connections

    # ── public API (called from CentralDispatcher thread) ──────────────────

    def schedule_forward(self, server_id: str, scenario_id: Optional[str],
                         msg: TaskMessage, dispatch_id: Optional[int] = None) -> None:
        """Schedule a forward on the WS loop (called from CentralDispatcher thread)."""
        asyncio.run_coroutine_threadsafe(
            self.forward_task(server_id, scenario_id, msg, dispatch_id), self._loop
        )

    def defer(self, server_id: str, scenario_id: Optional[str],
              msg: TaskMessage, dispatch_id: Optional[int] = None) -> None:
        """Park a task until the server reconnects. Does NOT ack the dispatch."""
        with self._lock:
            self.deferred.setdefault(server_id, []).append((scenario_id, msg, dispatch_id))
            n = len(self.deferred[server_id])
        logger.info(f"Deferred task {msg.task_id} for server {server_id} (parked={n})")

    # ── forward + reply correlation (runs on the WS loop) ──────────────────

    async def forward_task(self, server_id: str, scenario_id: Optional[str],
                           msg: TaskMessage, dispatch_id: Optional[int] = None) -> None:
        """Send a task to a server and await its result.

        Owns the full WS-path lifecycle: mark_started -> send -> await result ->
        finalize -> ack. On any connection failure, defer (no ack) for re-dispatch
        on reconnect.
        """
        task_repo = TaskRepository()

        # Idempotency: redelivery of an already-terminal task -> ack + skip.
        existing = await run_in_db_thread(task_repo.find_by_task_id, msg.task_id)
        if existing and existing.state in TASK_EXECUTION_COMPLETE_STATES:
            logger.info(f"forward_task: task {msg.task_id} already terminal "
                        f"({existing.state}); skipping redelivery")
            await run_in_db_thread(central_dispatcher._ack, dispatch_id, msg.task_id)
            return

        future = self._loop.create_future()
        with self._lock:
            self.pending[msg.task_id] = future
            self.task_server[msg.task_id] = server_id
            conn = self.connections.get(server_id)

        if conn is None:
            # disconnected between schedule_forward and run -> park
            with self._lock:
                self.pending.pop(msg.task_id, None)
                self.task_server.pop(msg.task_id, None)
            self.defer(server_id, scenario_id, msg, dispatch_id)
            return

        try:
            await run_in_db_thread(task_repo.mark_as_started, msg.task_id)
            await conn.ws.send_str(task_frame(
                msg.task_id, msg.parent_task_id, msg.goal, msg.context
            ))
            logger.info(f"Forwarded task {msg.task_id} to server {server_id}")
            start_time = datetime.now()
            # 等待结果但设上限：agent 心跳仍在却永不回结果时，超时按连接异常处理
            # （defer，不 ack），避免协程永久 await 夯死。
            result = await asyncio.wait_for(future, timeout=_TASK_FORWARD_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(f"forward_task: task {msg.task_id} to {server_id} "
                           f"timed out after {_TASK_FORWARD_TIMEOUT}s; deferring for reconnect")
            with self._lock:
                self.pending.pop(msg.task_id, None)
                self.task_server.pop(msg.task_id, None)
            self.defer(server_id, scenario_id, msg, dispatch_id)
            return
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
        elapsed = round((datetime.now() - start_time).total_seconds(), 3)
        agent_role = (result or {}).get("role") or msg.context.get("role")
        # finalize_task 内含多次 DB 写 + 场景查询，整体移到 DB 线程，不阻塞 loop。
        await run_in_db_thread(lambda: finalize_task(
            scenario_id, msg.task_id, result,
            agent_name=f"ExecutionServer:{server_id}",
            agent_role=agent_role,
            execution_duration=elapsed,
        ))
        # Ack the dispatch now that the task is finalized (ack-after-execute).
        await run_in_db_thread(central_dispatcher._ack, dispatch_id, msg.task_id)

    def _drain_deferred(self, server_id: str) -> None:
        """Re-dispatch tasks parked while a server was offline."""
        with self._lock:
            parked = self.deferred.pop(server_id, [])
        for scenario_id, msg, dispatch_id in parked:
            logger.info(f"Re-dispatching deferred task {msg.task_id} to {server_id}")
            self.schedule_forward(server_id, scenario_id, msg, dispatch_id)

    # ── app wiring ────────────────────────────────────────────────────────

    def _make_app(self) -> web.Application:
        """Build the aiohttp Application: WS routes + REST routes."""
        from auth.middleware import auth_middleware
        app = web.Application(middlewares=[auth_middleware])
        app.router.add_get("/", self.ws_handler)
        app.router.add_get("/subscribe", self.subscribe_handler)
        register_routes(app, self)
        return app

    async def subscribe_handler(self, request: web.Request) -> web.StreamResponse:
        """Backend event subscription endpoint — receives task_result broadcasts (tenant-isolated)."""
        from auth.ws_auth import authenticate_ws_connection

        # WebSocket 鉴权
        tenant_id = await authenticate_ws_connection(request)
        if not tenant_id:
            return web.Response(status=401, text="Unauthorized: Invalid or missing API key")

        ws = web.WebSocketResponse()
        if not ws.can_prepare(request).ok:
            return web.Response(text="WebSocket upgrade required", status=400)

        await ws.prepare(request)
        self.subscribers[ws] = tenant_id
        logger.info(f"后端订阅者已连接: {request.remote}, tenant={tenant_id} (总计: {len(self.subscribers)})")

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    logger.debug(f"收到订阅者消息: {msg.data[:100]}")
                elif msg.type == WSMsgType.ERROR:
                    logger.error(f"订阅者 WS 错误: {ws.exception()}")
        finally:
            del self.subscribers[ws]
            logger.info(f"后端订阅者已断开 (剩余: {len(self.subscribers)})")

        return ws

    # ── WS handler (GET /) ────────────────────────────────────────────────

    async def ws_handler(self, request: web.Request) -> web.StreamResponse:
        """WebSocket entry point: upgrade WS, reject plain HTTP with info (tenant-isolated)."""
        from auth.ws_auth import authenticate_ws_connection

        # 对于非 WebSocket 请求，返回 REST API 信息
        ws = web.WebSocketResponse()
        if not ws.can_prepare(request).ok:
            return web.Response(
                text="WebSocket Server.\n"
                     "  WS   : ws://<host>:<port>/?api_key=<token>  (执行服务器 / human agent)\n"
                     "  REST : GET /api/health | GET /api/servers | "
                     "POST /api/tasks/dispatch | DELETE /api/servers/{id}\n",
                status=200,
                content_type="text/plain",
            )

        # WebSocket 鉴权
        tenant_id = await authenticate_ws_connection(request)
        if not tenant_id:
            return web.Response(status=401, text="Unauthorized: Invalid or missing API key")

        await ws.prepare(request)

        server_id = None
        logger.info(f"新连接来自: {request.remote}, tenant={tenant_id}")
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        frame = P.parse_frame(msg.data)
                        frame_type = frame["type"]

                        if frame_type == TYPE_REGISTER:
                            server_id = await self._handle_register(ws, frame, tenant_id)
                        elif frame_type == TYPE_STATUS:
                            await self._handle_heartbeat(server_id, frame)
                        elif frame_type == TYPE_TASK_RESULT:
                            await self._handle_task_result(frame)
                        elif frame_type == TYPE_TASK_EVENT:
                            self._on_task_event(server_id, frame)
                        elif frame_type == TYPE_ACK:
                            # exec-server acked receipt of a dispatched task
                            ack_task_id = frame.get("task_id")
                            if ack_task_id and server_id:
                                # 索引直查（task_id），替代原先 find_unacked_dispatch(100) + 内存扫描
                                m = await run_in_db_thread(
                                    self.message_repo.find_unacked_dispatch_by_task_id,
                                    ack_task_id,
                                )
                                if m:
                                    await run_in_db_thread(self.message_repo.ack_message, m.id)
                                    logger.info(f"Task {ack_task_id} dispatch acked by {server_id}")
                        else:
                            logger.warning(f"未知帧类型: {frame_type}")
                    except ValueError as e:
                        logger.warning(f"Bad frame: {e}")
                        continue
                    except Exception as e:
                        logger.error(f"处理消息错误: {e}")
                        continue
                elif msg.type == WSMsgType.ERROR:
                    logger.error(f"WS 连接错误: {ws.exception()}")
                    break
        except Exception as e:
            logger.error(f"连接处理错误: {e}")
        finally:
            if server_id:
                await self._handle_disconnect(ws, server_id)
        return ws

    # ── WS message handlers ───────────────────────────────────────────────

    async def _handle_register(self, ws: web.WebSocketResponse,
                               frame: dict, tenant_id: str = None) -> Optional[str]:
        """Handle register request from exec-server or human agent."""
        payload = frame.get("payload", {})
        server_id = payload.get("server_id")

        if not server_id:
            logger.error("注册请求缺少 server_id")
            await ws.send_str(ack_frame(ok=False, error="missing server_id"))
            return None

        with self._lock:
            old = self.connections.get(server_id)
            if old is not None and old.ws is not ws:
                # ponytail: duplicate server_id -> kick old socket
                try:
                    await old.ws.close()
                except Exception:
                    pass
            now = datetime.now()
            conn = ConnectedServer(
                ws=ws,
                server_id=server_id,
                connected_at=now,
                last_heartbeat=now,
                tenant_id=tenant_id,
            )
            self.connections[server_id] = conn

        await run_in_db_thread(lambda: self.server_repo.upsert(
            server_id=server_id,
            name=payload.get("name", server_id),
            status=STATUS_IDLE,
            total_quota=payload.get("total_quota", 0),
            env_info=payload.get("env_info") or {},
            connected=True,
            source=payload.get("source") or "execution_server",
            tenant_id=tenant_id,
        ))

        await ws.send_str(ack_frame(ok=True))
        logger.info(f"服务器注册成功: {server_id} (quota={payload.get('total_quota', 0)}, tenant={tenant_id})")

        # Drain tasks parked while this server was offline
        self._drain_deferred(server_id)
        return server_id

    async def _handle_heartbeat(self, server_id: str, frame: dict):
        """Handle status heartbeat from a connected server."""
        if server_id not in self.connections:
            logger.warning(f"收到未知服务器的心跳: {server_id}")
            return

        conn = self.connections[server_id]
        conn.last_heartbeat = datetime.now()

        payload = frame.get("payload", {})
        status = payload.get("status", STATUS_IDLE)
        running_count = payload.get("running_count", 0)
        env_info = payload.get("env_info")

        if env_info is not None:
            await run_in_db_thread(lambda: self.server_repo.update_status(
                server_id, status, running_count=running_count,
                connected=True, env_info=env_info,
            ))
        else:
            await run_in_db_thread(lambda: self.server_repo.update_status(
                server_id, status, running_count=running_count, connected=True,
            ))

    def _on_task_event(self, server_id: Optional[str], frame: dict) -> None:
        """Handle task_event from execution servers (emits on event_bus)."""
        p = frame.get("payload", {})
        event = p.get("event")
        tid = frame.get("task_id")
        # 获取连接的 tenant_id
        tenant_id = None
        if server_id and server_id in self.connections:
            tenant_id = self.connections[server_id].tenant_id

        if event == EVENT_AGENT_CREATED:
            event_bus.emit("task.execution_agent_created", {
                "task_id": tid, "role": p.get("role"), "server_id": server_id,
                "tenant_id": tenant_id,
            })
        elif event == EVENT_TASK_STARTED:
            event_bus.emit("task.execution_started", {
                "task_id": tid, "server_id": server_id,
                "tenant_id": tenant_id,
            })
        else:
            logger.debug(f"task_event {event} for {tid}")

    async def _handle_task_result(self, frame: dict):
        """Handle task result: resolve pending future + broadcast to subscribers."""
        p = frame.get("payload", {})
        tid = p.get("task_id") or frame.get("task_id")
        result = p.get("result", {})

        if not tid:
            logger.error("任务结果缺少 task_id")
            return

        # Resolve in-flight future (from forward_task)
        with self._lock:
            fut = self.pending.get(tid)
            self.task_server.pop(tid, None)

        if fut is None:
            # ponytail: late result for a task already re-deferred/re-run after
            # a disconnect -> drop.
            logger.warning(f"task_result for {tid} with no pending future; dropping")
            return

        if not fut.done():
            fut.set_result(result)
            with self._lock:
                # Resolved -> no longer awaiting; forward_task holds its own
                # reference and its later pop is idempotent.
                self.pending.pop(tid, None)

        # Broadcast to subscribers
        await self._broadcast_event("task_result", p)

        outcome = "completed" if p.get("success") else "failed"
        logger.info(f"任务结果已转发: {tid} -> {outcome}")

    async def _broadcast_event(self, event_type: str, payload: dict):
        """Enqueue an event for fan-out to /subscribe subscribers.

        Non-blocking: callers (task_result handler, REST routes, event_bus hook)
        never wait on slow subscribers. A single worker drains the queue and sends
        with a per-subscriber timeout, so a stuck client can't block the WS
        server's event loop or starve other agents' frame processing.
        """
        q = self._broadcast_queue
        if q is None:
            return  # not started yet / shutting down
        try:
            q.put_nowait((event_type, payload))
        except asyncio.QueueFull:
            logger.warning(f"广播队列已满({_BROADCAST_QUEUE_MAXSIZE})，丢弃事件: {event_type}")

    async def _broadcast_worker(self):
        """Single-consumer fan-out: serializes per-subscriber sends (aiohttp
        WebSocketResponse is not safe to call concurrently) and bounds each send
        with a timeout so a slow subscriber can't stall the worker.

        Tenant-isolated: only sends events to subscribers of the same tenant.
        """
        q = self._broadcast_queue
        while True:
            event_type, payload = await q.get()
            try:
                if not self.subscribers:
                    continue
                # 获取事件的 tenant_id（如果存在）
                event_tenant = payload.get("tenant_id") if isinstance(payload, dict) else None
                frame = make_frame(TYPE_EVENT, {
                    "event_type": event_type,
                    "payload": payload,
                })
                dead = []
                for ws, subscriber_tenant in list(self.subscribers.items()):
                    # 租户隔离：只推送给同租户的订阅者
                    # 如果事件没有 tenant_id，则推送给所有订阅者（向后兼容）
                    if event_tenant and subscriber_tenant != event_tenant:
                        continue
                    try:
                        await asyncio.wait_for(
                            ws.send_str(frame), timeout=_BROADCAST_SEND_TIMEOUT
                        )
                        self._broadcast_fail.pop(ws, None)
                    except asyncio.TimeoutError:
                        n = self._broadcast_fail.get(ws, 0) + 1
                        self._broadcast_fail[ws] = n
                        logger.warning(f"订阅者推送超时({n}/{_BROADCAST_FAIL_THRESHOLD})，跳过: {event_type}")
                        if n >= _BROADCAST_FAIL_THRESHOLD:
                            dead.append(ws)
                    except Exception as e:
                        logger.warning(f"广播失败: {e}")
                        dead.append(ws)
                for ws in dead:
                    if ws in self.subscribers:
                        del self.subscribers[ws]
                    self._broadcast_fail.pop(ws, None)
                    try:
                        await ws.close()
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"广播 worker 异常: {e}")
            finally:
                q.task_done()

    async def _handle_disconnect(self, ws: web.WebSocketResponse, server_id: str):
        """Handle disconnect: fail in-flight futures + cleanup + mark offline."""
        with self._lock:
            # Only clear if this socket still owns the server_id slot
            # (a duplicate-id replacement may have already taken it over).
            conn = self.connections.get(server_id)
            if conn and conn.ws is not ws:
                return
            if conn:
                del self.connections[server_id]

            # Fail in-flight futures for this server -> forward_task will defer.
            to_fail = [tid for tid, s in self.task_server.items()
                       if s == server_id]
            for tid in to_fail:
                fut = self.pending.get(tid)
                if fut and not fut.done():
                    fut.set_exception(ConnectionLost(server_id))
            for tid in to_fail:
                self.pending.pop(tid, None)
                self.task_server.pop(tid, None)

        await run_in_db_thread(self.server_repo.mark_offline, server_id)
        logger.info(f"服务器断开连接: {server_id} "
                    f"(failing {len(to_fail)} in-flight task(s))")

    # ── heartbeat management ──────────────────────────────────────────────

    async def check_heartbeats(self):
        """Periodically close connections with stale heartbeats."""
        while True:
            await asyncio.sleep(5)
            now = datetime.now()
            timeout_servers = []
            for server_id, conn in self.connections.items():
                elapsed = (now - conn.last_heartbeat).total_seconds()
                if elapsed > self.heartbeat_timeout:
                    timeout_servers.append(server_id)

            for server_id in timeout_servers:
                logger.warning(f"服务器心跳超时: {server_id}")
                conn = self.connections.get(server_id)
                if conn:
                    try:
                        await conn.ws.close()
                    except Exception:
                        pass
                    del self.connections[server_id]
                await run_in_db_thread(self.server_repo.mark_offline, server_id)

    async def _heartbeat_sweeper(self) -> None:
        """Periodically mark DB-stale servers offline (DB-level sweep)."""
        interval = int(os.getenv("HEARTBEAT_INTERVAL", "5"))
        threshold = max(interval * 3, 10)
        while True:
            await asyncio.sleep(interval)
            try:
                stale_before = datetime.now() - timedelta(seconds=threshold)
                n = await run_in_db_thread(self.server_repo.mark_stale_offline, stale_before)
                if n:
                    logger.info(f"Heartbeat sweeper marked {n} stale server(s) offline")
            except Exception as e:
                logger.error(f"Heartbeat sweeper error: {e}")

    # ── lifecycle ─────────────────────────────────────────────────────────

    def run(self):
        """Start the server: manual loop for run_coroutine_threadsafe support."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop

        # aiohttp app runner (replaces web.run_app)
        runner = web.AppRunner(self.app)
        loop.run_until_complete(runner.setup())

        # TLS：配置了证书+私钥则端口以 wss/https 提供服务（单端口，与明文互斥）
        ssl_context = None
        scheme = "ws/http"
        if self.ssl_cert and self.ssl_key:
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(self.ssl_cert, self.ssl_key)
            scheme = "wss/https"
            logger.info(f"TLS enabled: cert={self.ssl_cert}")
        site = web.TCPSite(runner, self.host, self.port, ssl_context=ssl_context)
        loop.run_until_complete(site.start())
        logger.info(f"WebSocket + HTTP Server: {scheme}://{self.host}:{self.port}")

        # Start CentralDispatcher — wires cross-thread dispatch to this server
        central_dispatcher.set_ws_dispatcher(self)
        central_dispatcher.start()

        # Hook event_bus to forward events to WS subscribers
        self._hook_event_bus()

        # 广播 fan-out：单 worker 消费队列，按订阅者超时推送，慢消费者不阻塞调用方。
        self._broadcast_queue = asyncio.Queue(maxsize=_BROADCAST_QUEUE_MAXSIZE)
        loop.create_task(self._broadcast_worker())

        # 抑制互联网扫描器把 TLS 握手打到明文端口产生的 aiohttp bad-request ERROR 噪音
        _install_aiohttp_log_filter()

        # Heartbeat tasks on the WS loop
        loop.create_task(self.check_heartbeats())
        loop.create_task(self._heartbeat_sweeper())

        try:
            loop.run_forever()
        except KeyboardInterrupt:
            logger.info("Server shutting down...")
        finally:
            central_dispatcher.stop()
            loop.run_until_complete(runner.cleanup())
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()


# 抑制 aiohttp 对“畸形请求”的 ERROR 日志（互联网扫描器把 TLS 握手 / 超长行打到
# 明文 HTTP 端口时产生）。这些请求无害且会被 aiohttp 自动 400 关闭，但默认按
# ERROR 打印整条 traceback 会刷屏并淹没真实错误；真实异常仍照常记录。
_AIOHTTP_BADREQ_MARKERS = (
    "BadHttpMethod", "BadStatusLine", "Invalid method",
    "invalid HTTP method", "Line is too long",
)


def _install_aiohttp_log_filter() -> None:
    """Attach a filter to the aiohttp.server logger that drops bad-request noise."""
    import traceback as _tb

    class _BadReqFilter(logging.Filter):
        def filter(self, record):
            text = record.getMessage()
            if record.exc_info:
                text = text + "\n" + "".join(_tb.format_exception(*record.exc_info))
            return not any(m in text for m in _AIOHTTP_BADREQ_MARKERS)

    logging.getLogger("aiohttp.server").addFilter(_BadReqFilter())


def main():
    """Entry point."""
    config = load_config()
    init_database()

    ws_server = WebSocketServer(
        host=config["host"],
        port=config["port"],
        heartbeat_timeout=config.get("heartbeat_timeout", 15),
        ssl_cert=config.get("ssl_cert"),
        ssl_key=config.get("ssl_key"),
    )
    ws_server.run()


if __name__ == "__main__":
    main()