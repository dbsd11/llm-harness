# WS client - connects to the backend, registers, heartbeats, runs recv loop.
import asyncio
import ssl
import threading

import websockets

from logger import logger
from core import local_ws_protocol as P
from .env_probe import probe_env


class WSClient:
    """Connects to the backend WS server with auto-reconnect."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        from common.utils.local_global_loop_util import get_random_work_loop
        self.loop = get_random_work_loop()
        self._ws = None
        self._api_key = cfg.get("api_key", "")
        self.task_runner = None  # set by run()
        self._stop = threading.Event()

    def send(self, data: str) -> None:
        """Thread-safe send: schedule the send on the WS loop."""
        if self._ws is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._ws.send(data), self.loop)
        except Exception as e:
            logger.warning(f"WS send failed: {e}")

    def run(self, task_runner) -> None:
        """Block: connect, register, heartbeat, and dispatch incoming tasks.

        Schedules _main on the shared worker loop from global_loop_util
        (already running in a daemon thread), then blocks the calling thread
        until shutdown.
        """
        self.task_runner = task_runner
        main_fut = asyncio.run_coroutine_threadsafe(self._main(), self.loop)
        main_fut.result()  # blocks until _main returns (_stop set)

    async def _main(self) -> None:
        backoff = 1
        url = self.cfg["backend_ws_url"]
        while not self._stop.is_set():
            try:
                logger.info(f"Connecting to backend WS: {url}")
                # wss:// 自签名证书：客户端跳过校验；ws:// 不传 ssl
                _ssl = ssl._create_unverified_context() if url.startswith("wss") else None
                _headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else None
                async with websockets.connect(url, max_size=None, ssl=_ssl, additional_headers=_headers) as ws:
                    self._ws = ws
                    await ws.send(P.register_frame(
                        self.cfg["server_id"],
                        self.cfg["server_name"],
                        self.cfg["max_quota"],
                        probe_env(),
                        source="execution_server",
                    ))
                    logger.info(f"Registered as {self.cfg['server_id']} "
                                f"(quota={self.cfg['max_quota']})")
                    hb = asyncio.ensure_future(self._heartbeat())
                    try:
                        await self._recv_loop(ws)
                    finally:
                        hb.cancel()
            except Exception as e:
                logger.warning(f"WS connection lost: {e}")
            finally:
                self._ws = None

            if self._stop.is_set():
                break
            logger.info(f"Reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _recv_loop(self, ws) -> None:
        async for raw in ws:
            try:
                frame = P.parse_frame(raw)
            except ValueError as e:
                logger.warning(f"Bad frame: {e}")
                continue
            t = frame["type"]
            if t == P.TYPE_TASK:
                p = frame["payload"]
                self.task_runner.submit(
                    p.get("task_id", ""),
                    p.get("parent_task_id", ""),
                    p.get("goal", ""),
                    p.get("context", {}),
                )
            elif t == P.TYPE_ACK:
                ack = frame["payload"]
                if not ack.get("ok"):
                    logger.warning(f"Backend NACK: {ack.get('error')}")
            else:
                logger.debug(f"Exec server received frame: {t}")

    async def _heartbeat(self) -> None:
        interval = self.cfg["heartbeat_interval"]
        while True:
            await asyncio.sleep(interval)
            rc = self.task_runner.running_count
            status = P.STATUS_RUNNING if rc > 0 else P.STATUS_IDLE
            self.send(P.status_frame(
                status, self.cfg["max_quota"], rc, probe_env()
            ))
