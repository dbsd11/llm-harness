"""Task management REST API routes for ws_server (aiohttp)."""

import json
from datetime import datetime
from aiohttp import web

from database.repositories.task_repository import TaskRepository
from scenarios.scenario_manager import scenario_manager
from logger import logger
from common.utils.global_loop_util import run_in_db_thread


def _iso(v) -> str:
    return v.isoformat() if isinstance(v, datetime) else v


def _task_to_dict(t) -> dict:
    return {
        "task_id": t.task_id,
        "agent_run_id": t.agent_run_id,
        "scenario_id": t.scenario_id,
        "parent_task_id": t.parent_task_id,
        "topic_id": t.topic_id,
        "idempotency_key": t.idempotency_key,
        "depends_on": json.loads(t.depends_on) if t.depends_on else [],
        "goal": t.goal,
        "state": t.state,
        "priority": t.priority,
        "timeout_seconds": t.timeout_seconds,
        "max_retries": t.max_retries,
        "retry_count": t.retry_count,
        "agent_name": t.agent_name,
        "agent_role": t.agent_role,
        "execution_duration": t.execution_duration,
        "review_feedback": t.review_feedback,
        "context": json.loads(t.context) if t.context else {},
        "result": json.loads(t.result) if t.result else None,
        "error": t.error,
        "created_at": _iso(t.created_at) if t.created_at else None,
        "updated_at": _iso(t.updated_at) if t.updated_at else None,
        "started_at": _iso(t.started_at) if t.started_at else None,
        "completed_at": _iso(t.completed_at) if t.completed_at else None,
    }


async def list_tasks(request: web.Request) -> web.Response:
    """List tasks with optional filters."""
    scenario_id = request.query.get("scenario_id")
    state = request.query.get("state")
    limit = int(request.query.get("limit", "100"))

    try:
        repo = TaskRepository()
        if scenario_id:
            tasks = await run_in_db_thread(repo.find_by_scenario_id, scenario_id)
        elif state:
            tasks = await run_in_db_thread(repo.find_by_state, state)
        else:
            tasks = await run_in_db_thread(lambda: repo.find_all(limit=limit))

        return web.json_response({
            "success": True,
            "tasks": [_task_to_dict(t) for t in tasks],
            "total": len(tasks),
        })
    except Exception as e:
        logger.error(f"Failed to list tasks: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def get_task(request: web.Request) -> web.Response:
    """Get a single task by ID."""
    task_id = request.match_info["task_id"]
    try:
        repo = TaskRepository()
        task = await run_in_db_thread(repo.find_by_task_id, task_id)
        if not task:
            return web.json_response({"success": False, "error": "Task not found"}, status=404)
        return web.json_response({"success": True, "task": _task_to_dict(task)})
    except Exception as e:
        logger.error(f"Failed to get task: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def cancel_task(request: web.Request) -> web.Response:
    """Cancel/delete a task (only non-terminal)."""
    task_id = request.match_info["task_id"]
    try:
        from core.state_machine import TASK_TERMINAL_STATES
        repo = TaskRepository()
        task = await run_in_db_thread(repo.find_by_task_id, task_id)
        if not task:
            return web.json_response({"success": False, "error": "Task not found"}, status=404)
        if task.state in TASK_TERMINAL_STATES:
            return web.json_response({
                "success": False,
                "error": f"Cannot cancel task in terminal state: {task.state}",
            }, status=400)
        await run_in_db_thread(repo.mark_as_cancelled, task_id)
        return web.json_response({"success": True, "message": f"Task {task_id} cancelled"})
    except Exception as e:
        logger.error(f"Failed to cancel task: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def accept_task(request: web.Request) -> web.Response:
    """Human acceptance review for a gated task."""
    task_id = request.match_info["task_id"]
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    passed = payload.get("passed", True)
    feedback = payload.get("feedback", "")

    ok = await run_in_db_thread(scenario_manager.review_task, task_id, passed, feedback)
    if not ok:
        return web.json_response({"success": False, "error": "Review failed"}, status=400)

    ws_server = request.app.get("ws_server")
    if ws_server:
        await ws_server._broadcast_event("task_reviewed", {
            "task_id": task_id, "passed": passed, "feedback": feedback,
        })

    return web.json_response({"success": True, "message": f"Task {task_id} reviewed"})


def register_task_routes(app: web.Application) -> None:
    app.router.add_get("/api/tasks", list_tasks)
    app.router.add_get("/api/tasks/{task_id}", get_task)
    app.router.add_delete("/api/tasks/{task_id}", cancel_task)
    app.router.add_post("/api/tasks/{task_id}/accept", accept_task)