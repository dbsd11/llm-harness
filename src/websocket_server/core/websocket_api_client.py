# WebSocket Server API Client - 调用远程 WebSocket Server 的 REST API
import os
import requests
from logger import logger

# 远程 WebSocket Server API 地址
# 默认使用 localhost:8765，因为所有服务部署在同一台机器上
# WebSocket Server 的 REST API 和 WebSocket 共用 8765 端口
WS_SERVER_API_URL = os.getenv("WS_SERVER_API_URL", "http://agent-socket-server.bdzz.com.cn:8765")


def _make_request(method, endpoint, **kwargs):
    """发送 HTTP 请求到远程 API"""
    url = f"{WS_SERVER_API_URL}{endpoint}"
    try:
        response = requests.request(method, url, timeout=5, **kwargs)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"API 请求失败: {method} {url} - {e}")
        return {"success": False, "error": str(e)}


def list_servers():
    """列出所有执行服务器"""
    return _make_request("GET", "/api/servers")


def get_server(server_id):
    """获取单个服务器详情"""
    return _make_request("GET", f"/api/servers/{server_id}")


def delete_server(server_id):
    """删除服务器（仅离线服务器）"""
    return _make_request("DELETE", f"/api/servers/{server_id}")


def cleanup_offline():
    """清理所有离线服务器"""
    return _make_request("POST", "/api/servers/offline")


def health_check():
    """健康检查"""
    return _make_request("GET", "/api/health")


def dispatch_task(task_id: str, server_id: str, goal: str, context: dict,
                  scenario_id: str = None, parent_task_id: str = ""):
    """分发任务到执行服务器

    Args:
        task_id: 任务 ID
        server_id: 目标执行服务器 ID
        goal: 任务目标
        context: 任务上下文
        scenario_id: 场景 ID（可选）
        parent_task_id: 父任务 ID（可选）

    Returns:
        dict: API 响应结果
    """
    payload = {
        "task_id": task_id,
        "server_id": server_id,
        "goal": goal,
        "context": context,
        "parent_task_id": parent_task_id,
    }
    if scenario_id:
        payload["scenario_id"] = scenario_id

    return _make_request("POST", "/api/tasks/dispatch", json=payload)
