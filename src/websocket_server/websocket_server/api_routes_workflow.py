"""Workflow management REST API routes for ws_server (aiohttp)."""

import json
from datetime import datetime
from aiohttp import web

from database.repositories.workflow_repository import WorkflowRepository
from database.repositories.workflow_task_template_repository import WorkflowTaskTemplateRepository
from services.workflow_publisher import workflow_publisher
from services.workflow_executor import workflow_executor
from logger import logger
from common.utils.global_loop_util import run_in_db_thread


def _iso(v) -> str:
    return v.isoformat() if isinstance(v, datetime) else v


def _workflow_to_dict(w) -> dict:
    return {
        "workflow_id": w.workflow_id,
        "name": w.name,
        "description": w.description,
        "source_scenario_id": w.source_scenario_id,
        "dag_definition": json.loads(w.dag_definition) if w.dag_definition else {},
        "input_schema": json.loads(w.input_schema) if w.input_schema else {},
        "agent_roles": json.loads(w.agent_roles) if w.agent_roles else {},
        "experience_context": json.loads(w.experience_context) if w.experience_context else [],
        "version": w.version,
        "state": w.state,
        "created_by": w.created_by,
        "created_at": _iso(w.created_at),
        "updated_at": _iso(w.updated_at),
    }


async def publish_workflow(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)

    scenario_id = payload.get("scenario_id")
    if not scenario_id:
        return web.json_response({"success": False, "error": "scenario_id is required"}, status=400)

    name = payload.get("name")
    description = payload.get("description")
    created_by = payload.get("created_by")

    try:
        result = await run_in_db_thread(
            workflow_publisher.publish, scenario_id, name, description, created_by)

        ws_server = request.app.get("ws_server")
        if ws_server:
            await ws_server._broadcast_event("workflow_published", {
                "workflow_id": result["workflow_id"],
                "name": result["name"],
            })

        return web.json_response({"success": True, "workflow": result})
    except ValueError as e:
        return web.json_response({"success": False, "error": str(e)}, status=400)
    except Exception as e:
        logger.error(f"Failed to publish workflow: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def list_workflows(request: web.Request) -> web.Response:
    state = request.query.get("state")
    limit = int(request.query.get("limit", "100"))

    try:
        repo = WorkflowRepository()
        if state:
            workflows = await run_in_db_thread(lambda: repo.find_by_state(state, limit=limit))
        else:
            workflows = await run_in_db_thread(lambda: repo.find_all(limit=limit))

        return web.json_response({
            "success": True,
            "workflows": [_workflow_to_dict(w) for w in workflows],
            "total": len(workflows),
        })
    except Exception as e:
        logger.error(f"Failed to list workflows: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def get_workflow(request: web.Request) -> web.Response:
    workflow_id = request.match_info["workflow_id"]
    try:
        repo = WorkflowRepository()
        wf = await run_in_db_thread(repo.find_by_workflow_id, workflow_id)
        if not wf:
            return web.json_response({"success": False, "error": "Workflow not found"}, status=404)

        template_repo = WorkflowTaskTemplateRepository()
        templates = await run_in_db_thread(template_repo.find_by_workflow_id, workflow_id)

        result = _workflow_to_dict(wf)
        result["templates"] = [
            {
                "template_id": t.template_id,
                "step_id": t.step_id,
                "goal_template": t.goal_template,
                "depends_on": json.loads(t.depends_on) if t.depends_on else [],
                "agent_role": t.agent_role,
                "server_id": t.server_id,
                "timeout_seconds": t.timeout_seconds,
                "input_param_mapping": json.loads(t.input_param_mapping) if t.input_param_mapping else {},
                "experience_note": json.loads(t.experience_note) if t.experience_note else {},
                "step_order": t.step_order,
            }
            for t in templates
        ]

        return web.json_response({"success": True, "workflow": result})
    except Exception as e:
        logger.error(f"Failed to get workflow: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def update_workflow(request: web.Request) -> web.Response:
    workflow_id = request.match_info["workflow_id"]
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)

    try:
        repo = WorkflowRepository()
        wf = await run_in_db_thread(repo.find_by_workflow_id, workflow_id)
        if not wf:
            return web.json_response({"success": False, "error": "Workflow not found"}, status=404)

        if "name" in payload:
            wf.name = payload["name"]
        if "description" in payload:
            wf.description = payload["description"]
        if "state" in payload and payload["state"] in ("draft", "active", "archived"):
            wf.state = payload["state"]
        wf.updated_at = datetime.now()

        await run_in_db_thread(repo.update, wf)
        return web.json_response({"success": True, "workflow": _workflow_to_dict(wf)})
    except Exception as e:
        logger.error(f"Failed to update workflow: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def delete_workflow(request: web.Request) -> web.Response:
    workflow_id = request.match_info["workflow_id"]
    try:
        repo = WorkflowRepository()
        template_repo = WorkflowTaskTemplateRepository()

        await run_in_db_thread(template_repo.delete_by_workflow_id, workflow_id)
        ok = await run_in_db_thread(repo.delete_by_workflow_id, workflow_id)
        if not ok:
            return web.json_response({"success": False, "error": "Workflow not found"}, status=404)

        return web.json_response({"success": True, "message": f"Workflow {workflow_id} deleted"})
    except Exception as e:
        logger.error(f"Failed to delete workflow: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def execute_workflow(request: web.Request) -> web.Response:
    workflow_id = request.match_info["workflow_id"]
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)

    input_params = payload.get("input_params", {})
    created_by = payload.get("created_by")

    try:
        result = await run_in_db_thread(
            workflow_executor.execute, workflow_id, input_params, created_by)

        ws_server = request.app.get("ws_server")
        if ws_server:
            await ws_server._broadcast_event("workflow_execution_started", {
                "scenario_id": result["scenario_id"],
                "workflow_id": workflow_id,
            })

        return web.json_response({"success": True, "execution": result})
    except ValueError as e:
        return web.json_response({"success": False, "error": str(e)}, status=400)
    except Exception as e:
        logger.error(f"Failed to execute workflow: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


def register_workflow_routes(app: web.Application) -> None:
    app.router.add_post("/api/workflows/publish", publish_workflow)
    app.router.add_get("/api/workflows", list_workflows)
    app.router.add_get("/api/workflows/{workflow_id}", get_workflow)
    app.router.add_put("/api/workflows/{workflow_id}", update_workflow)
    app.router.add_delete("/api/workflows/{workflow_id}", delete_workflow)
    app.router.add_post("/api/workflows/{workflow_id}/execute", execute_workflow)
