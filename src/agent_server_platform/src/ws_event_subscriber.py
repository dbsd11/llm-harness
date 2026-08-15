"""WS 事件订阅客户端 - 连接到 WS 服务器订阅 task_result 等事件"""
import os
import json
import asyncio
import ssl
from logger import logger
from database.repositories.task_repository import TaskRepository
from database.repositories.event_repository import EventRepository
from database.repositories.human_task_repository import HumanTaskRepository


class WSEventSubscriber:
    """订阅 WS 服务器的事件，更新本地数据库"""

    def __init__(self):
        self.ws_url = os.getenv("WS_SERVER_WS_URL", "wss://agent-socket-server.bdzz.com.cn:8765/subscribe")
        # ponytail: ensure /subscribe path so we receive broadcast events,
        # not agent task frames. If the env var omits the path, add it.
        if not self.ws_url.endswith("/subscribe"):
            self.ws_url = self.ws_url.rstrip("/") + "/subscribe"
        self.api_key = os.getenv("WS_SERVER_API_KEY", "")
        self.task_repo = TaskRepository()
        self.event_repo = EventRepository()
        self._loop = None
        self._task = None
        self._ws = None
        self._running = False

    def start(self):
        """启动订阅 - 跑在 global_loop_util 的 worker loop 上（与 Gradio/uvicorn 的
        main loop 隔离，但不再自建线程+loop，统一由 global_loop_util 管理）。"""
        if self._running:
            logger.warning("WS 订阅已在运行")
            return

        self._running = True
        from common.utils.local_global_loop_util import get_random_work_loop
        self._loop = get_random_work_loop()
        self._task = asyncio.run_coroutine_threadsafe(self._subscribe_loop(), self._loop)
        logger.info(f"WS 事件订阅已启动: {self.ws_url}")

    def stop(self):
        """停止订阅 - 取消订阅任务，但不停止共享的 worker loop（其它任务可能共用）。"""
        self._running = False
        if self._ws and self._loop:
            try:
                asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
            except Exception as e:
                logger.warning(f"关闭 WS 连接失败: {e}")
        if self._task:
            self._task.cancel()
        logger.info("WS 事件订阅已停止")

    async def _subscribe_loop(self):
        """订阅循环 - 连接 WS 服务器并处理事件"""
        import websockets

        backoff = 1
        while self._running:
            try:
                logger.info(f"连接到 WS 订阅端点: {self.ws_url}")
                _ssl = ssl._create_unverified_context() if self.ws_url.startswith("wss") else None
                _headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else None
                async with websockets.connect(self.ws_url, ssl=_ssl, additional_headers=_headers) as ws:
                    self._ws = ws
                    backoff = 1
                    logger.info("WS 订阅已连接")

                    async for message in ws:
                        try:
                            data = json.loads(message)
                            await self._handle_event(data)
                        except Exception as e:
                            logger.error(f"处理事件失败: {e}")

            except Exception as e:
                logger.warning(f"WS 订阅断开: {e}")
                self._ws = None

                if self._running:
                    logger.info(f"{backoff} 秒后重连...")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)

    async def _handle_event(self, data: dict):
        """处理收到的事件 — 所有事件统一写入本地 events 表。"""
        frame_type = data.get("type")

        if frame_type == "event":
            payload = data.get("payload", {})
            event_type = payload.get("event_type")
            inner = payload.get("payload", {})
            trace_id = payload.get("trace_id", "")

            # Write ALL events to local events table (platform's only persistent store)
            import json as _json
            from common.utils.local_global_loop_util import run_in_db_thread
            await run_in_db_thread(
                self.event_repo.create_event,
                event_type,
                _json.dumps(inner, ensure_ascii=False) if isinstance(inner, dict) else str(inner),
                trace_id or "",
                _json.dumps(payload.get("metadata", {}), ensure_ascii=False) if payload.get("metadata") else "{}",
            )

            # Special handling for task_result (update local task state for scheduling agent)
            if event_type == "task_result":
                await self._handle_task_result(inner)
            elif event_type == "task_dispatched":
                await self._handle_task_dispatched(inner)
            elif event_type == "human_task_submitted":
                # Update human_task status locally
                await run_in_db_thread(
                    self._persist_human_task_submitted, inner.get("task_id", "")
                )
        else:
            logger.debug(f"收到未知帧类型: {frame_type}")

    async def _handle_task_result(self, payload: dict):
        """处理任务结果事件"""
        task_id = payload.get("task_id")
        result = payload.get("result")
        success = payload.get("success")

        if not task_id:
            logger.error("task_result 事件缺少 task_id")
            return

        # DB 写入是同步 I/O（走进程级共享 SQLite 连接）。放到专用 DB 线程池中执行，
        # 避免阻塞订阅器的 event loop。同时不与 uvicorn 默认线程池竞争（Gradio 的
        # /theme.css 等 sync 路由也走默认线程池）。
        # _subscribe_loop 逐条 await，故写库天然串行，无需额外加锁。
        from common.utils.local_global_loop_util import run_in_db_thread
        await run_in_db_thread(self._persist_task_result, task_id, result, success)
        if success:
            logger.info(f"任务完成: {task_id}")
        else:
            error_msg = result.get("error", "Unknown error") if isinstance(result, dict) else str(result)
            logger.info(f"任务失败: {task_id} - {error_msg}")

    async def _handle_task_dispatched(self, payload: dict):
        """Store a task dispatched to a human agent in the local DB."""
        task_id = payload.get("task_id")
        server_id = payload.get("server_id")
        if not task_id or not server_id:
            logger.warning(f"task_dispatched event missing task_id or server_id: {payload}")
            return
        from common.utils.local_global_loop_util import run_in_db_thread
        await run_in_db_thread(self._persist_task_dispatched, task_id, server_id,
                                payload.get("goal", ""),
                                payload.get("context", {}),
                                payload.get("parent_task_id", ""))

    def _persist_task_dispatched(self, task_id: str, server_id: str,
                                 goal: str, context: dict,
                                 parent_task_id: str) -> None:
        """Sync DB write for dispatched task."""
        repo = HumanTaskRepository()
        repo.create_table_if_not_exists()
        row_id = repo.create_task(task_id, server_id, goal, context, parent_task_id)
        if row_id:
            logger.info(f"Stored human task {task_id} for server {server_id}")
        else:
            logger.debug(f"Human task {task_id} already stored; skipping")

    def _persist_task_result(self, task_id: str, result, success: bool) -> None:
        """同步写库 - 在线程中执行，避免阻塞订阅 loop。"""
        # 更新本地任务状态
        if success:
            result_str = json.dumps(result) if result else None
            self.task_repo.mark_as_completed(task_id, result=result_str)
        else:
            error_msg = result.get("error", "Unknown error") if isinstance(result, dict) else str(result)
            self.task_repo.mark_as_failed(task_id, error=error_msg)

        # Write a reply message so the scheduling agent can collect the result
        # and advance to the next wave. Without this, collect_replies times out
        # and dependent tasks stay pending forever.
        from database.repositories.message_repository import MessageRepository
        from database.models.local_message import Message
        from datetime import datetime
        msg_repo = MessageRepository()
        task = self.task_repo.find_by_task_id(task_id)
        scenario_id = task.scenario_id if task else ""
        reply = Message(
            scenario_id=scenario_id,
            task_id=task_id,
            from_agent="execution",
            to_agent="scheduling",
            message_type="reply",
            content=json.dumps({
                "task_id": task_id,
                "success": success,
                "result": result if isinstance(result, dict) else {"output": str(result)},
            }, ensure_ascii=False),
            timestamp=datetime.now(),
        )
        msg_repo.create(reply)

        # 记录事件
        event_data = json.dumps({
            "task_id": task_id,
            "success": success,
            "result": result
        }, ensure_ascii=False)
        self.event_repo.create_event("task.result_received", event_data)

    def _persist_human_task_submitted(self, task_id: str) -> None:
        """Mark a human task as submitted in local DB."""
        if not task_id:
            return
        repo = HumanTaskRepository()
        repo.mark_submitted(task_id)


# 全局单例
_ws_subscriber = None

def get_ws_subscriber() -> WSEventSubscriber:
    """获取全局 WS 订阅客户端实例"""
    global _ws_subscriber
    if _ws_subscriber is None:
        _ws_subscriber = WSEventSubscriber()
    return _ws_subscriber
