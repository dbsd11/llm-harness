"""WebSocket Server REST API - 执行服务器查询/管理 + 任务分发 (aiohttp)

注册到主 aiohttp 应用上，与 WebSocket 共用同一端口 (8765)。
所有处理函数为协程，通过 app["ws_server"] 访问 WS 服务器实例。
"""
import json
from datetime import datetime

from aiohttp import web

from database.models.message import Message
from database.repositories.execution_server_repository import ExecutionServerRepository
from database.repositories.message_repository import MessageRepository
from logger import logger
# 同步 DB I/O 移出 aiohttp event loop，避免 REST 处理阻塞 WS 服务。
from common.utils.global_loop_util import run_in_db_thread


def _iso(v) -> str:
    """datetime -> ISO 字符串 (aiohttp json_response 不自动序列化 datetime，
    而 Flask jsonify 会，迁移后需显式转换以保持响应兼容)"""
    return v.isoformat() if isinstance(v, datetime) else v


def _server_to_dict(s, ws_server) -> dict:
    """将 ExecutionServer 模型转为 JSON 字典，附加实时连接状态"""
    return {
        "server_id": s.server_id,
        "name": s.name,
        "status": s.status,
        "connected": s.server_id in ws_server.connections,
        "total_quota": s.total_quota,
        "running_count": s.running_count,
        "env_info": s.env_info,
        "last_heartbeat": _iso(s.last_heartbeat),
        "updated_at": _iso(s.updated_at),
        "source": getattr(s, "source", "execution_server"),
    }


async def health(request: web.Request) -> web.Response:
    """健康检查"""
    ws_server = request.app["ws_server"]
    return web.json_response({
        "status": "ok",
        "connected_servers": len(ws_server.connections),
    })


async def list_servers(request: web.Request) -> web.Response:
    """列出所有执行服务器"""
    ws_server = request.app["ws_server"]
    try:
        repo = ExecutionServerRepository()
        servers = await run_in_db_thread(repo.list_all)
        result = [_server_to_dict(s, ws_server) for s in servers]
        return web.json_response({
            "success": True,
            "servers": result,
            "total": len(result),
        })
    except Exception as e:
        logger.error(f"列出服务器失败: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def get_server(request: web.Request) -> web.Response:
    """获取单个服务器详情"""
    ws_server = request.app["ws_server"]
    server_id = request.match_info["server_id"]
    try:
        repo = ExecutionServerRepository()
        server = await run_in_db_thread(repo.find_by_server_id, server_id)

        if not server:
            return web.json_response(
                {"success": False, "error": f"服务器 {server_id} 不存在"}, status=404
            )

        return web.json_response({
            "success": True,
            "server": _server_to_dict(server, ws_server),
        })
    except Exception as e:
        logger.error(f"获取服务器详情失败: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def cleanup_offline(request: web.Request) -> web.Response:
    """清理所有离线服务器"""
    try:
        repo = ExecutionServerRepository()
        deleted_count = await run_in_db_thread(repo.delete_offline)

        return web.json_response({
            "success": True,
            "deleted_count": deleted_count,
            "message": f"已清理 {deleted_count} 个离线服务器",
        })
    except Exception as e:
        logger.error(f"清理离线服务器失败: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def delete_server(request: web.Request) -> web.Response:
    """删除指定服务器 (仅离线可删)"""
    ws_server = request.app["ws_server"]
    server_id = request.match_info["server_id"]
    try:
        # 守门：在线服务器不可删 (删后镜像与活连接分叉)
        if server_id in ws_server.connections:
            return web.json_response(
                {"success": False, "error": f"服务器 {server_id} 当前在线，无法删除"},
                status=400,
            )

        repo = ExecutionServerRepository()
        success = await run_in_db_thread(repo.delete, server_id)

        if success:
            return web.json_response({"success": True, "message": f"已删除服务器 {server_id}"})
        return web.json_response(
            {"success": False, "error": f"服务器 {server_id} 不存在"}, status=404
        )
    except Exception as e:
        logger.error(f"删除服务器失败: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def dispatch_task(request: web.Request) -> web.Response:
    """分发任务到执行服务器

    写入一条 dispatch 消息行 (acked=0)。CentralDispatcher 在下次轮询时
    (每 0.5s 检查未 ack 行) 自动拾取并通过 WS 转发 TASK 帧。
    content JSON 必须包含 server_id (用于路由匹配) 以及 goal/context/task_id。
    """
    try:
        payload = await request.json()
    except Exception:
        return web.json_response(
            {"success": False, "error": "无效的 JSON 请求体"}, status=400
        )

    task_id = payload.get("task_id")
    server_id = payload.get("server_id")
    goal = payload.get("goal", "")
    context = payload.get("context", {}) or {}
    parent_task_id = payload.get("parent_task_id", "")
    scenario_id = payload.get("scenario_id")

    if not task_id or not server_id:
        return web.json_response(
            {"success": False, "error": "task_id 和 server_id 为必填"}, status=400
        )

    try:
        content = json.dumps({
            "task_id": task_id,
            "server_id": server_id,
            "goal": goal,
            "context": context,
            "parent_task_id": parent_task_id,
            "scenario_id": scenario_id,
        }, ensure_ascii=False)

        msg = Message(
            scenario_id=scenario_id or "",
            task_id=task_id,
            from_agent="scheduling",
            to_agent="execution",
            message_type="dispatch",
            content=content,
            acked=0,
            timestamp=datetime.now(),
        )
        repo = MessageRepository()
        message_id = await run_in_db_thread(repo.create, msg)

        # CentralDispatcher picks up unacked dispatch rows on its next poll
        # (every 0.5s when pending). No explicit trigger needed.
        logger.info(f"任务已写入分发队列: task_id={task_id} server_id={server_id} msg_id={message_id}")
        return web.json_response({
            "success": True,
            "message_id": message_id,
            "task_id": task_id,
            "server_id": server_id,
        })
    except Exception as e:
        logger.error(f"分发任务失败: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


def register_routes(app: web.Application, ws_server) -> None:
    """注册 REST API 路由到 aiohttp 应用"""
    app["ws_server"] = ws_server
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/servers", list_servers)
    app.router.add_get("/api/servers/{server_id}", get_server)
    app.router.add_post("/api/servers/offline", cleanup_offline)
    app.router.add_delete("/api/servers/{server_id}", delete_server)
    app.router.add_post("/api/tasks/dispatch", dispatch_task)

    # Register scenario, task, chat, event, agent, tool routes
    from .api_routes_scenario import register_scenario_routes
    from .api_routes_task import register_task_routes
    from .api_routes_chat import register_chat_routes
    from .api_routes_event import register_event_routes
    from .api_routes_agent import register_agent_routes
    from .api_routes_tool import register_tool_routes

    register_scenario_routes(app)
    register_task_routes(app)
    register_chat_routes(app)
    register_event_routes(app)
    register_agent_routes(app)
    register_tool_routes(app)
    logger.info("All API routes registered")
