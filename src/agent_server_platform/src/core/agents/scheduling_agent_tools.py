# Scheduling Agent Tools - tools for execution server management
import json
from core.agents.tool_registry import tool_registry
from core.sandbox_manager import SandboxManager
from database.repositories.execution_server_repository import ExecutionServerRepository
from logger import logger


def list_execution_servers() -> dict:
    """
    Tool: List all registered execution agent servers.

    Returns:
        {
            "servers": [
                {
                    "server_id": str,
                    "name": str,
                    "status": str,
                    "connected": bool,
                    "total_quota": int,
                    "running_count": int,
                    "source": str,
                    "last_heartbeat": str (ISO format or None)
                }
            ],
            "total": int
        }
    """
    repo = ExecutionServerRepository()
    servers = repo.list_all()

    result = {
        "servers": [
            {
                "server_id": s.server_id,
                "name": s.name,
                "status": s.status,
                "connected": s.connected,
                "total_quota": s.total_quota,
                "running_count": s.running_count,
                "source": s.source,
                "last_heartbeat": s.last_heartbeat.isoformat() if s.last_heartbeat else None,
                "env_info": json.loads(s.env_info) if s.env_info and s.env_info != "{}" else {},
            }
            for s in servers
        ],
        "total": len(servers),
    }

    logger.info(f"Listed {result['total']} execution servers")
    return result


def create_execution_server_sandbox(
    server_id: str,
    server_name: str = None,
    max_quota: int = 4,
    image: str = None,
    backend_ws_url: str = None,
) -> dict:
    """
    Tool: Create an execution agent server sandbox.

    Args:
        server_id: Unique server identifier
        server_name: Display name (optional, defaults to server_id)
        max_quota: Maximum concurrent tasks (default: 4)
        image: Docker image name (optional, uses default from config)
        backend_ws_url: Backend WebSocket URL (optional, uses default from config)

    Returns:
        {
            "success": bool,
            "server_id": str,
            "sandbox_id": str (container ID),
            "message": str
        }
    """
    manager = SandboxManager()

    config = {
        "server_id": server_id,
        "server_name": server_name or server_id,
        "max_quota": max_quota,
        "image": image,
        "backend_ws_url": backend_ws_url,
    }

    try:
        result = manager.create_sandbox(config)
        logger.info(f"Created sandbox for server {server_id}: {result['container_id']}")

        return {
            "success": True,
            "server_id": server_id,
            "sandbox_id": result.get("container_id"),
            "message": "Execution server sandbox created successfully",
        }
    except Exception as e:
        logger.error(f"Failed to create sandbox for {server_id}: {e}")
        return {
            "success": False,
            "server_id": server_id,
            "error": str(e),
        }


def destroy_execution_server_sandbox(server_id: str) -> dict:
    """
    Tool: Destroy an execution agent server sandbox.

    Args:
        server_id: Server identifier

    Returns:
        {
            "success": bool,
            "server_id": str,
            "message": str
        }
    """
    manager = SandboxManager()

    try:
        success = manager.destroy_sandbox(server_id)

        if success:
            logger.info(f"Destroyed sandbox for server {server_id}")
            return {
                "success": True,
                "server_id": server_id,
                "message": "Sandbox destroyed successfully",
            }
        else:
            return {
                "success": False,
                "server_id": server_id,
                "message": "Sandbox not found",
            }
    except Exception as e:
        logger.error(f"Failed to destroy sandbox for {server_id}: {e}")
        return {
            "success": False,
            "server_id": server_id,
            "error": str(e),
        }


# Register tools
tool_registry.register(
    name="list_execution_servers",
    func=list_execution_servers,
    description="查询已注册的执行Agent Server列表，返回服务器ID、名称、状态、配额等信息",
)

tool_registry.register(
    name="create_execution_server_sandbox",
    func=create_execution_server_sandbox,
    description="创建执行Agent Server沙箱并启动Agent Server",
    parameters={
        "server_id": {"type": "string", "description": "服务器唯一标识"},
        "server_name": {"type": "string", "description": "服务器名称", "required": False},
        "max_quota": {"type": "integer", "description": "最大并发任务数", "default": 4},
        "image": {"type": "string", "description": "Docker镜像名称", "required": False},
        "backend_ws_url": {"type": "string", "description": "后端WebSocket地址", "required": False},
    },
)

tool_registry.register(
    name="destroy_execution_server_sandbox",
    func=destroy_execution_server_sandbox,
    description="销毁执行Agent Server沙箱",
    parameters={
        "server_id": {"type": "string", "description": "服务器唯一标识"},
    },
)
