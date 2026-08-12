# HumanAgentClient — WS client that mimics the execution-agent-server protocol.
#
# Connects to the remote WS server, registers as a human agent, receives task
# frames, persists them to the local human_tasks table, and sends task_result
# frames when a human worker submits an answer through the Gradio UI.
#
# From the WS server's perspective this is identical to an execution-agent
# server — same register/heartbeat/task/task_result protocol. The difference:
# instead of executing tasks via LLM, tasks are saved to the DB and displayed
# on the Gradio human_tasks page for a human operator to complete.
import os
import asyncio
import ssl
from datetime import datetime

from logger import logger
from core import local_ws_protocol as P
from database.repositories.human_task_repository import HumanTaskRepository


class HumanAgentClient:
    """WS client for human-agent task routing.

    Single instance (global singleton) per process. Runs on a worker loop
    managed by global_loop_util, same as the ws_event_subscriber.
    """

    def __init__(self):
        self._server_id = os.getenv("HUMAN_AGENT_DEFAULT_ID", "human-1")
        self._total_quota = int(os.getenv("HUMAN_AGENT_QUOTA", "8"))
        # Agent connection uses root path (not /subscribe)
        self._ws_url = os.getenv("WS_SERVER_WS_URL", "wss://agent-socket-server.bdzz.com.cn:8765")
        self._ws_url = self._ws_url.replace("/subscribe", "")
        self._heartbeat_interval = int(os.getenv("HUMAN_HEARTBEAT_INTERVAL", "5"))
        self._api_key = os.getenv("WS_SERVER_API_KEY", "")

        self._loop = None
        self._task = None
        self._ws = None
        self._running = False
        self._repo = HumanTaskRepository()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        """Start the client on a worker loop (non-blocking).

        Uses global_loop_util's worker pool — same pattern as
        WSEventSubscriber. Safe to call multiple times (idempotent).
        """
        if self._running:
            return

        self._running = True
        self._repo.create_table_if_not_exists()

        from common.utils.local_global_loop_util import get_random_work_loop
        self._loop = get_random_work_loop()
        self._task = asyncio.run_coroutine_threadsafe(self._main(), self._loop)
        logger.info(f"HumanAgentClient started: server_id={self._server_id} "
                     f"quota={self._total_quota} url={self._ws_url}")

    def stop(self):
        """Stop the client — cancel the WS loop task, close the connection."""
        self._running = False
        if self._ws and self._loop:
            try:
                asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
            except Exception as e:
                logger.warning(f"HumanAgentClient close WS failed: {e}")
        if self._task:
            self._task.cancel()
        logger.info("HumanAgentClient stopped")

    def send_result(self, task_id: str, success: bool, output: str) -> bool:
        """Thread-safe. Send a task_result frame back to the WS server.

        Called from the Gradio event handler (main thread). Enqueues the
        send onto the client's worker loop. Also marks the local human_tasks
        row as submitted.

        Returns True if the send was enqueued, False if the client is not
        connected (caller should warn the user).
        """
        if not self._ws or not self._loop:
            logger.warning(f"HumanAgentClient not connected, cannot send result for {task_id}")
            return False

        result = {"success": success, "output": output, "role": "human",
                   "question": "", "submitted_at": datetime.now().isoformat()}
        frame = P.task_result_frame(task_id, success, result)

        # Mark submitted locally first (sync — runs on Gradio thread)
        self._repo.mark_submitted(task_id)

        # Enqueue the WS send onto the worker loop
        async def _send():
            try:
                await self._ws.send(frame)
                logger.info(f"HumanAgentClient sent task_result for {task_id}")
            except Exception as e:
                logger.error(f"HumanAgentClient send_result failed for {task_id}: {e}")

        asyncio.run_coroutine_threadsafe(_send(), self._loop)
        return True

    # ------------------------------------------------------------------
    # Internal — connection lifecycle
    # ------------------------------------------------------------------

    async def _main(self):
        """Connect, register, heartbeat, and receive loop with auto-reconnect."""
        import websockets

        backoff = 1
        # wss:// 自签名证书：客户端跳过校验；ws:// 不传 ssl
        _ssl = ssl._create_unverified_context() if self._ws_url.startswith("wss") else None
        while self._running:
            try:
                logger.info(f"HumanAgentClient connecting to {self._ws_url}")
                _headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else None
                async with websockets.connect(
                    self._ws_url, max_size=None, ssl=_ssl,
                    extra_headers=_headers,
                ) as ws:
                    self._ws = ws
                    backoff = 1

                    # Register
                    await ws.send(P.register_frame(
                        self._server_id,
                        self._server_id,  # name = server_id for human agents
                        self._total_quota,
                        {"type": "human_agent"},
                        source="human_agent",
                    ))
                    logger.info(f"HumanAgentClient registered as {self._server_id}")

                    # Heartbeat + receive loop
                    hb = asyncio.ensure_future(self._heartbeat())
                    try:
                        await self._recv_loop(ws)
                    finally:
                        hb.cancel()
                        try:
                            await hb  # let cancellation propagate
                        except (asyncio.CancelledError, Exception):
                            pass

                    logger.info("HumanAgentClient connection ended, reconnecting...")

            except Exception as e:
                logger.warning(f"HumanAgentClient connection lost: {e}")
            finally:
                self._ws = None

            if not self._running:
                break
            logger.info(f"HumanAgentClient reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _recv_loop(self, ws):
        """Receive frames from the WS server. On 'task' frames, persist to DB."""
        import websockets
        while True:
            try:
                raw = await ws.recv()
            except websockets.exceptions.ConnectionClosedOK:
                logger.info("HumanAgentClient: server closed connection OK")
                return
            except websockets.exceptions.ConnectionClosedError as e:
                logger.warning(f"HumanAgentClient: connection closed with error: {e}")
                return
            except Exception as e:
                logger.error(f"HumanAgentClient recv error: {type(e).__name__}: {e}")
                return

            try:
                frame = P.parse_frame(raw)
            except ValueError as e:
                logger.warning(f"HumanAgentClient bad frame: {e}")
                continue

            t = frame["type"]
            if t == P.TYPE_TASK:
                payload = frame["payload"]
                task_id = payload.get("task_id", "")
                goal = payload.get("goal", "")
                context = payload.get("context", {})
                parent_task_id = payload.get("parent_task_id", "")

                # DB write is sync I/O; run in dedicated DB thread pool to
                # avoid blocking the WS event loop AND avoid competing with
                # Gradio's default thread pool (/theme.css sync routes).
                from common.utils.local_global_loop_util import run_in_db_thread
                await run_in_db_thread(
                    self._persist_task, task_id, goal, context, parent_task_id
                )

                # Immediately ack receipt so the WS server marks the dispatch
                # as delivered. If we crash before this ack, the WS server
                # will re-send on the next heartbeat — no task loss.
                await ws.send(P.ack_frame(ok=True, task_id=task_id))
                logger.info(f"HumanAgentClient received + acked task {task_id}")

            elif t == P.TYPE_ACK:
                ack = frame["payload"]
                if not ack.get("ok"):
                    logger.warning(f"HumanAgentClient NACK: {ack.get('error')}")
                else:
                    logger.info(f"HumanAgentClient registered OK (ack received)")

            else:
                logger.debug(f"HumanAgentClient received frame: {t}")

    async def _heartbeat(self):
        """Send periodic status frames."""
        logger.info(f"HumanAgentClient heartbeat started (interval={self._heartbeat_interval}s)")
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            # ponytail: don't use asyncio.to_thread for the DB query — the default
            # executor is shared with Gradio's sync route handlers (/theme.css etc.)
            # and can stall when the thread pool is saturated.
            try:
                await self._ws.send(P.status_frame(
                    P.STATUS_IDLE, self._total_quota, 0,
                    {"type": "human_agent"},
                ))
                logger.info(f"HumanAgentClient heartbeat sent")
            except Exception as e:
                logger.warning(f"HumanAgentClient heartbeat send failed: {e}")
                break
        logger.info("HumanAgentClient heartbeat stopped")

    # ------------------------------------------------------------------
    # Internal — DB persistence
    # ------------------------------------------------------------------

    def _persist_task(self, task_id: str, goal: str, context: dict,
                      parent_task_id: str):
        """Sync DB write for a received task (runs in thread via to_thread)."""
        row_id = self._repo.create_task(
            task_id, self._server_id, goal, context, parent_task_id
        )
        if row_id:
            logger.info(f"HumanAgentClient stored task {task_id} for {self._server_id}")
        else:
            logger.debug(f"HumanAgentClient task {task_id} already stored; skipping")


# ------------------------------------------------------------------
# Global singleton
# ------------------------------------------------------------------

_human_agent_client = None


def get_human_agent_client() -> HumanAgentClient:
    global _human_agent_client
    if _human_agent_client is None:
        _human_agent_client = HumanAgentClient()
    return _human_agent_client