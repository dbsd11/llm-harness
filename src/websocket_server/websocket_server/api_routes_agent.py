"""Agent registry REST API for ws_server (aiohttp)."""

import json
from datetime import datetime
from aiohttp import web

from database.repositories.agent_repository import AgentRepository
from logger import logger
from common.utils.global_loop_util import run_in_db_thread


def _iso(v) -> str:
    return v.isoformat() if isinstance(v, datetime) else v


def _agent_to_dict(a) -> dict:
    cfg = {}
    if a.config:
        try:
            cfg = json.loads(a.config)
        except (json.JSONDecodeError, TypeError):
            cfg = {}
    return {
        "agent_id": a.agent_id,
        "scenario_id": a.scenario_id,
        "agent_type": a.agent_type,
        "name": a.name,
        "description": a.description,
        "config": cfg,
        "status": a.status,
        "created_at": _iso(a.created_at) if a.created_at else None,
        "updated_at": _iso(a.updated_at) if a.updated_at else None,
    }


async def list_agents(request: web.Request) -> web.Response:
    """List agents with optional filters (tenant-isolated)."""
    agent_type = request.query.get("type")
    status = request.query.get("status")
    limit = int(request.query.get("limit", "200"))
    tenant_id = request.get("tenant_id")
    if not tenant_id:
        return web.json_response({"success": False, "error": "tenant_id required"}, status=401)

    try:
        repo = AgentRepository()
        if agent_type:
            agents = await run_in_db_thread(lambda: repo.find_by_type(agent_type, tenant_id=tenant_id))
        else:
            agents = await run_in_db_thread(lambda: repo.find_all_by_tenant(tenant_id, limit=limit))

        if status and status != "all":
            agents = [a for a in agents if a.status == status]

        return web.json_response({
            "success": True,
            "agents": [_agent_to_dict(a) for a in agents],
            "total": len(agents),
        })
    except Exception as e:
        logger.error(f"Failed to list agents: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def get_agent(request: web.Request) -> web.Response:
    """Get a single agent by ID (tenant-isolated)."""
    agent_id = request.match_info["agent_id"]
    tenant_id = request.get("tenant_id")
    try:
        repo = AgentRepository()
        agent = await run_in_db_thread(repo.find_by_agent_id, agent_id)
        if not agent:
            return web.json_response({"success": False, "error": "Agent not found"}, status=404)

        if tenant_id and agent.tenant_id and agent.tenant_id != tenant_id:
            return web.json_response({"success": False, "error": "Agent not found"}, status=404)

        return web.json_response({"success": True, "agent": _agent_to_dict(agent)})
    except Exception as e:
        logger.error(f"Failed to get agent: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


def register_agent_routes(app: web.Application) -> None:
    app.router.add_get("/api/agents", list_agents)
    app.router.add_get("/api/agents/{agent_id}", get_agent)