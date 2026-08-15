# Scheduling Agent Tools - tools for execution server management
import json
from core.agents.tool_registry import tool_registry
from core.sandbox_manager import SandboxManager
from database.repositories.execution_server_repository import ExecutionServerRepository
from database.repositories.task_repository import TaskRepository
from logger import logger


def list_execution_servers(tenant_id: str) -> dict:
    """
    Tool: List all registered execution agent servers for the current tenant.

    Args:
        tenant_id: Tenant ID for isolation (injected by scheduling agent)

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
    servers = repo.find_all_by_tenant(tenant_id)

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


def get_task_details(task_id: str, tenant_id: str) -> dict:
    """
    Tool: Get detailed information about a specific task.

    Args:
        task_id: Task UUID
        tenant_id: Tenant ID for isolation (injected by scheduling agent)

    Returns:
        Task details including goal, state, result, context, depends_on
    """
    repo = TaskRepository()
    task = repo.find_by_task_id(task_id)
    if not task:
        return {"success": False, "error": f"Task not found: {task_id}"}
    if getattr(task, 'tenant_id', None) and task.tenant_id != tenant_id:
        return {"success": False, "error": f"Task not found: {task_id}"}

    return {
        "success": True,
        "task": {
            "task_id": task.task_id,
            "goal": task.goal,
            "state": task.state,
            "result": task.result,
            "error": task.error,
            "depends_on": json.loads(task.depends_on) if task.depends_on else [],
            "context": json.loads(task.context) if task.context else {},
            "priority": task.priority,
            "retry_count": task.retry_count,
            "execution_duration": task.execution_duration,
            "review_feedback": task.review_feedback,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        },
    }


def list_tasks_by_scenario(scenario_id: str, tenant_id: str) -> dict:
    """
    Tool: List all tasks in a scenario.

    Args:
        scenario_id: Scenario ID
        tenant_id: Tenant ID for isolation (injected by scheduling agent)

    Returns:
        List of tasks with id, goal, state, depends_on
    """
    repo = TaskRepository()
    tasks = repo.find_by_scenario_id(scenario_id, tenant_id=tenant_id)

    return {
        "success": True,
        "scenario_id": scenario_id,
        "tasks": [
            {
                "task_id": t.task_id,
                "goal": t.goal,
                "state": t.state,
                "depends_on": json.loads(t.depends_on) if t.depends_on else [],
                "priority": t.priority,
            }
            for t in tasks
        ],
        "total": len(tasks),
    }


def list_tasks_by_state(state: str, tenant_id: str) -> dict:
    """
    Tool: List all tasks in a specific state.

    Args:
        state: Task state (pending, running, success, failed, etc.)
        tenant_id: Tenant ID for isolation (injected by scheduling agent)

    Returns:
        List of tasks in the given state
    """
    repo = TaskRepository()
    tasks = repo.find_by_state(state, tenant_id=tenant_id)

    return {
        "success": True,
        "state": state,
        "tasks": [
            {
                "task_id": t.task_id,
                "goal": t.goal,
                "scenario_id": t.scenario_id,
                "depends_on": json.loads(t.depends_on) if t.depends_on else [],
            }
            for t in tasks
        ],
        "total": len(tasks),
    }


def get_server_details(server_id: str, tenant_id: str) -> dict:
    """
    Tool: Get detailed information about a specific execution server.

    Args:
        server_id: Server identifier
        tenant_id: Tenant ID for isolation (injected by scheduling agent)

    Returns:
        Server details including status, quota, env_info
    """
    repo = ExecutionServerRepository()
    server = repo.find_by_server_id(server_id)
    if not server:
        return {"success": False, "error": f"Server not found: {server_id}"}
    if getattr(server, 'tenant_id', None) and server.tenant_id != tenant_id:
        return {"success": False, "error": f"Server not found: {server_id}"}

    return {
        "success": True,
        "server": {
            "server_id": server.server_id,
            "name": server.name,
            "status": server.status,
            "connected": server.connected,
            "total_quota": server.total_quota,
            "running_count": server.running_count,
            "source": server.source,
            "env_info": json.loads(server.env_info) if server.env_info and server.env_info != "{}" else {},
            "last_heartbeat": server.last_heartbeat.isoformat() if server.last_heartbeat else None,
        },
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

tool_registry.register(
    name="get_task_details",
    func=get_task_details,
    description="查询指定任务的详细信息，包括目标、状态、结果、上下文、依赖等",
    parameters={
        "task_id": {"type": "string", "description": "任务UUID"},
    },
)

tool_registry.register(
    name="list_tasks_by_scenario",
    func=list_tasks_by_scenario,
    description="列出指定场景下的所有任务",
    parameters={
        "scenario_id": {"type": "string", "description": "场景ID"},
    },
)

tool_registry.register(
    name="list_tasks_by_state",
    func=list_tasks_by_state,
    description="按状态查询任务列表（如 pending/running/success/failed）",
    parameters={
        "state": {"type": "string", "description": "任务状态"},
    },
)

tool_registry.register(
    name="get_server_details",
    func=get_server_details,
    description="查询指定执行服务器的详细信息，包括状态、配额、环境信息等",
    parameters={
        "server_id": {"type": "string", "description": "服务器唯一标识"},
    },
)
