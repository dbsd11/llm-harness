"""Scenario management REST API routes for ws_server (aiohttp)."""

import json
import uuid
from datetime import datetime
from aiohttp import web

from database.repositories.scenario_repository import ScenarioRepository
from database.models.scenario import Scenario
from core.state_machine import ScenarioState
from core.event_bus import event_bus
from core.agents.agent_manager import agent_manager
from scenarios.scenario_manager import scenario_manager
from scenarios.examples.simple_qa_scenario import SimpleQAScenario
from scenarios.examples.code_execution_scenario import CodeExecutionScenario
from logger import logger
from common.utils.global_loop_util import run_in_db_thread


def _iso(v) -> str:
    return v.isoformat() if isinstance(v, datetime) else v


def _scenario_to_dict(s) -> dict:
    return {
        "scenario_id": s.scenario_id,
        "scenario_type": s.scenario_type,
        "name": s.name,
        "description": s.description,
        "state": s.state,
        "config": json.loads(s.config) if s.config else {},
        "context": json.loads(s.context) if s.context else {},
        "created_by": s.created_by,
        "created_at": _iso(s.created_at) if s.created_at else None,
        "updated_at": _iso(s.updated_at) if s.updated_at else None,
        "started_at": _iso(s.started_at) if s.started_at else None,
        "completed_at": _iso(s.completed_at) if s.completed_at else None,
    }


async def create_scenario(request: web.Request) -> web.Response:
    """Create a new scenario (tenant-isolated)."""
    tenant_id = request.get("tenant_id")
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)

    scenario_type = payload.get("scenario_type")
    name = payload.get("name", "")
    description = payload.get("description", "")
    config = payload.get("config", {})
    created_by = payload.get("created_by")

    if scenario_type not in ("simple_qa", "code_execution"):
        return web.json_response(
            {"success": False, "error": f"Invalid scenario_type: {scenario_type}"},
            status=400,
        )
    if not name.strip():
        return web.json_response({"success": False, "error": "name is required"}, status=400)

    try:
        scenario_id = await run_in_db_thread(
            scenario_manager.create_scenario,
            scenario_type, name, description, config, created_by,
        )

        # 设置 tenant_id
        if tenant_id:
            repo = ScenarioRepository()
            await run_in_db_thread(repo.update_tenant_id, scenario_id, tenant_id)

        # Broadcast event
        ws_server = request.app.get("ws_server")
        if ws_server:
            await ws_server._broadcast_event("scenario_created", {
                "scenario_id": scenario_id, "scenario_type": scenario_type,
                "name": name, "state": "initializing", "tenant_id": tenant_id,
            })

        scenario = await run_in_db_thread(ScenarioRepository().find_by_scenario_id, scenario_id)
        return web.json_response({
            "success": True,
            "scenario": _scenario_to_dict(scenario) if scenario else {"scenario_id": scenario_id},
        })
    except Exception as e:
        logger.error(f"Failed to create scenario: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def list_scenarios(request: web.Request) -> web.Response:
    """List all scenarios with optional state filter (tenant-isolated)."""
    state = request.query.get("state")
    limit = int(request.query.get("limit", "100"))
    tenant_id = request.get("tenant_id")

    try:
        repo = ScenarioRepository()
        if state:
            scenarios = await run_in_db_thread(lambda: repo.find_by_state(state, limit=limit, tenant_id=tenant_id))
        elif tenant_id:
            scenarios = await run_in_db_thread(lambda: repo.find_all_by_tenant(tenant_id, limit=limit))
        else:
            scenarios = await run_in_db_thread(lambda: repo.find_all(limit=limit))

        return web.json_response({
            "success": True,
            "scenarios": [_scenario_to_dict(s) for s in scenarios],
            "total": len(scenarios),
        })
    except Exception as e:
        logger.error(f"Failed to list scenarios: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def get_scenario(request: web.Request) -> web.Response:
    """Get a single scenario by ID (tenant-isolated)."""
    scenario_id = request.match_info["scenario_id"]
    tenant_id = request.get("tenant_id")
    try:
        repo = ScenarioRepository()
        scenario = await run_in_db_thread(repo.find_by_scenario_id, scenario_id)
        if not scenario:
            return web.json_response({"success": False, "error": "Scenario not found"}, status=404)

        # 租户隔离：检查所有权
        if tenant_id and scenario.tenant_id and scenario.tenant_id != tenant_id:
            return web.json_response({"success": False, "error": "Scenario not found"}, status=404)

        return web.json_response({"success": True, "scenario": _scenario_to_dict(scenario)})
    except Exception as e:
        logger.error(f"Failed to get scenario: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def start_scenario(request: web.Request) -> web.Response:
    """Start a scenario (tenant-isolated)."""
    scenario_id = request.match_info["scenario_id"]
    tenant_id = request.get("tenant_id")
    try:
        repo = ScenarioRepository()
        scenario = await run_in_db_thread(repo.find_by_scenario_id, scenario_id)
        if not scenario:
            return web.json_response({"success": False, "error": "Scenario not found"}, status=404)

        # 租户隔离：检查所有权
        if tenant_id and scenario.tenant_id and scenario.tenant_id != tenant_id:
            return web.json_response({"success": False, "error": "Scenario not found"}, status=404)

        if scenario.state != ScenarioState.INITIALIZING.value:
            return web.json_response({
                "success": False,
                "error": f"Can only start from 'initializing', current state: {scenario.state}",
            }, status=400)

        config = json.loads(scenario.config) if scenario.config else {}
        stype = scenario.scenario_type

        if stype == "simple_qa":
            instance = SimpleQAScenario()
        elif stype == "code_execution":
            instance = CodeExecutionScenario()
        else:
            return web.json_response({
                "success": False, "error": f"Unknown scenario type: {stype}",
            }, status=400)

        ok = await run_in_db_thread(scenario_manager.start_scenario, scenario_id, instance)
        if not ok:
            return web.json_response({"success": False, "error": "Failed to start scenario"}, status=500)

        ws_server = request.app.get("ws_server")
        if ws_server:
            await ws_server._broadcast_event("scenario_started", {
                "scenario_id": scenario_id, "tenant_id": tenant_id,
            })

        return web.json_response({"success": True, "message": f"Scenario {scenario_id} started"})
    except Exception as e:
        logger.error(f"Failed to start scenario: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def stop_scenario(request: web.Request) -> web.Response:
    """Stop a scenario (tenant-isolated)."""
    scenario_id = request.match_info["scenario_id"]
    tenant_id = request.get("tenant_id")
    try:
        # 租户隔离：先检查所有权
        if tenant_id:
            repo = ScenarioRepository()
            scenario = await run_in_db_thread(repo.find_by_scenario_id, scenario_id)
            if scenario and scenario.tenant_id and scenario.tenant_id != tenant_id:
                return web.json_response({"success": False, "error": "Scenario not found"}, status=404)

        ok = await run_in_db_thread(scenario_manager.stop_scenario, scenario_id)
        if not ok:
            return web.json_response({"success": False, "error": "Scenario not found"}, status=404)

        ws_server = request.app.get("ws_server")
        if ws_server:
            await ws_server._broadcast_event("scenario_stopped", {
                "scenario_id": scenario_id, "tenant_id": tenant_id,
            })

        return web.json_response({"success": True, "message": f"Scenario {scenario_id} stopped"})
    except Exception as e:
        logger.error(f"Failed to stop scenario: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def delete_scenario(request: web.Request) -> web.Response:
    """Delete a scenario (must be stopped first, tenant-isolated)."""
    scenario_id = request.match_info["scenario_id"]
    tenant_id = request.get("tenant_id")
    try:
        repo = ScenarioRepository()
        scenario = await run_in_db_thread(repo.find_by_scenario_id, scenario_id)
        if not scenario:
            return web.json_response({"success": False, "error": "Scenario not found"}, status=404)

        # 租户隔离：检查所有权
        if tenant_id and scenario.tenant_id and scenario.tenant_id != tenant_id:
            return web.json_response({"success": False, "error": "Scenario not found"}, status=404)

        # Only allow deletion of non-running scenarios
        if scenario.state in (ScenarioState.RUNNING.value,):
            return web.json_response({
                "success": False,
                "error": f"Cannot delete scenario in '{scenario.state}' state. Stop it first.",
            }, status=400)

        await run_in_db_thread(repo.delete_by_scenario_id, scenario_id)
        return web.json_response({"success": True, "message": f"Scenario {scenario_id} deleted"})
    except Exception as e:
        logger.error(f"Failed to delete scenario: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def get_scenario_messages(request: web.Request) -> web.Response:
    """Get message history timeline for a scenario (tenant-isolated)."""
    scenario_id = request.match_info["scenario_id"]
    tenant_id = request.get("tenant_id")
    try:
        repo = ScenarioRepository()
        scenario = await run_in_db_thread(repo.find_by_scenario_id, scenario_id)
        if not scenario:
            return web.json_response({"success": False, "error": "Scenario not found"}, status=404)

        # 租户隔离：检查所有权
        if tenant_id and scenario.tenant_id and scenario.tenant_id != tenant_id:
            return web.json_response({"success": False, "error": "Scenario not found"}, status=404)

        trace_id = ""
        if scenario.context:
            try:
                trace_id = json.loads(scenario.context).get("trace_id", "") or ""
            except (json.JSONDecodeError, TypeError):
                pass

        from core.export_html import build_message_history
        entries = await run_in_db_thread(build_message_history, scenario_id, trace_id, scenario)
        return web.json_response({
            "success": True,
            "messages": entries,
            "total": len(entries),
        })
    except Exception as e:
        logger.error(f"Failed to get scenario messages: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


def register_scenario_routes(app: web.Application) -> None:
    app.router.add_post("/api/scenarios", create_scenario)
    app.router.add_get("/api/scenarios", list_scenarios)
    app.router.add_get("/api/scenarios/{scenario_id}", get_scenario)
    app.router.add_get("/api/scenarios/{scenario_id}/messages", get_scenario_messages)
    app.router.add_post("/api/scenarios/{scenario_id}/start", start_scenario)
    app.router.add_post("/api/scenarios/{scenario_id}/stop", stop_scenario)
    app.router.add_delete("/api/scenarios/{scenario_id}", delete_scenario)