"""统一事件循环提供方。

架构：
- ``_global_loop_``（main loop）：运行 uvicorn ASGI 服务 + Gradio queue。HTTP 服务、队列
  处理、startup events 全部在同一个 loop 上，避免“HTTP loop 与 queue loop 分裂”导致
  多标签下 ``/theme.css`` 等 sync 路由被饿死。
- ``_work_loops_``（worker loops）：运行其它 asyncio 任务（WS 订阅器、agent 执行等），
  按轮询分配。

所有 loop 通过 ``new_event_loop()`` 显式创建（而非 ``get_event_loop()``，避免
deprecation 及“副作用式”抢占主线程 loop），并在各自守护线程上 ``set_event_loop`` 后
``run_forever``，使该线程上 ``get_event_loop()`` 也能返回正确 loop（兼容 uvicorn /
legacy 代码）。
"""
import os
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

from logger import logger


def _run_loop_forever(loop: asyncio.AbstractEventLoop, name: str) -> None:
    """在守护线程上运行指定 loop。先 ``set_event_loop`` 再 ``run_forever``，
    保证该线程内 ``get_event_loop()`` 返回此 loop。"""
    asyncio.set_event_loop(loop)
    try:
        loop.run_forever()
    except Exception as e:  # pragma: no cover - 线程内异常兜底
        logger.error(f"事件循环 {name} 发生异常: {e}")
    finally:
        logger.info(f"事件循环 {name} 结束")


# main loop —— 跑 Gradio/uvicorn HTTP 服务 + queue（单一 loop，杜绝分裂）
_global_loop_ = asyncio.new_event_loop()
global_loop_thread = threading.Thread(
    target=_run_loop_forever, args=(_global_loop_, "global"), daemon=True, name="global-loop"
)
global_loop_thread.start()

# worker loops —— 跑其它 asyncio 任务。先创建并 append，再启动线程，避免
# ``get_random_work_loop()`` 在线程尚未 append 时取到空列表导致 ``% 0``。
_work_loops_: list[asyncio.AbstractEventLoop] = []
for i in range(max(os.cpu_count() - 1, 1)):
    work_loop = asyncio.new_event_loop()
    _work_loops_.append(work_loop)
    threading.Thread(
        target=_run_loop_forever, args=(work_loop, f"work-{i}"), daemon=True, name=f"work-loop-{i}"
    ).start()


def get_global_loop() -> asyncio.AbstractEventLoop:
    """主 loop（运行 Gradio/uvicorn）。"""
    return _global_loop_


def get_random_work_loop() -> asyncio.AbstractEventLoop:
    """轮询取一个 worker loop（运行 WS 订阅、agent 执行等异步任务）。"""
    if not hasattr(get_random_work_loop, "_idx"):
        get_random_work_loop._idx = 0
    idx = get_random_work_loop._idx
    get_random_work_loop._idx = (idx + 1) % len(_work_loops_)
    return _work_loops_[idx]


# ── dedicated DB I/O executor ───────────────────────────────────────────
#
# asyncio.to_thread() uses the default ThreadPoolExecutor — the SAME pool
# that uvicorn/Gradio uses for sync route handlers (/theme.css etc.). When
# DB operations saturate the default pool, sync routes stall.
#
# This dedicated executor isolates blocking DB I/O so the default pool is
# always free for Gradio's sync route handlers.
# ponytail: raised default from 4 → 10 to match SQLite pool size; tune via env.
# Previously 4 workers contended on a single SQLite lock, saturating under load.
_DB_MAX_WORKERS = int(os.getenv("DB_THREAD_POOL_SIZE", "10"))
_db_executor = ThreadPoolExecutor(
    max_workers=_DB_MAX_WORKERS, thread_name_prefix="db-io"
)


async def run_in_db_thread(fn, *args):
    """Run a sync function in the dedicated DB I/O thread pool.

    Use this instead of asyncio.to_thread() for any DB read/write that
    would contend with uvicorn's default thread pool.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_db_executor, fn, *args)


# ── dedicated LLM I/O executor ───────────────────────────────────────────
#
# LLM 调用（同步 OpenAI 客户端，单次可达数十甚至上百秒）绝不能跑在 event loop 上。
# 单独线程池，与 DB I/O 线程池隔离：LLM 长耗时不会饿死 DB 写入，DB 写入也不会拖住
# LLM，二者更不会阻塞 aiohttp 主 loop。这是“不同功能走不同线程、互不阻塞”的体现。
_LLM_MAX_WORKERS = int(os.getenv("LLM_THREAD_POOL_SIZE", "4"))
_llm_executor = ThreadPoolExecutor(
    max_workers=_LLM_MAX_WORKERS, thread_name_prefix="llm-io"
)


async def run_in_llm_thread(fn, *args):
    """在专用 LLM 线程池中运行同步函数（如 llm_client.chat / scene_assistant.reply）。

    LLM 调用为同步阻塞 I/O，放专用线程池隔离，避免阻塞 event loop 与 DB 线程池。
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_llm_executor, fn, *args)
