"""Event query REST API for ws_server (aiohttp)."""

import json
from datetime import datetime
from aiohttp import web

from database.repositories.event_repository import EventRepository
from logger import logger
from common.utils.global_loop_util import run_in_db_thread


def _iso(v) -> str:
    return v.isoformat() if isinstance(v, datetime) else v


async def list_events(request: web.Request) -> web.Response:
    """List events with optional type/trace_id filters (tenant-isolated)."""
    event_type = request.query.get("event_type")
    trace_id = request.query.get("trace_id")
    limit = int(request.query.get("limit", "100"))
    tenant_id = request.get("tenant_id")

    try:
        repo = EventRepository()
        if event_type:
            events = await run_in_db_thread(lambda: repo.find_by_type(event_type, limit=limit, tenant_id=tenant_id))
        elif trace_id:
            events = await run_in_db_thread(lambda: repo.find_by_trace(trace_id, limit=limit, tenant_id=tenant_id))
        elif tenant_id:
            events = await run_in_db_thread(lambda: repo.find_all_by_tenant(tenant_id, limit=limit))
        else:
            events = await run_in_db_thread(lambda: repo.find_all(limit=limit))

        result = []
        for e in events:
            d = {
                "id": e.id,
                "event_type": e.event_type,
                "data": e.data,
                "trace_id": e.trace_id,
                "metadata": e.metadata,
                "timestamp": _iso(e.timestamp) if e.timestamp else None,
            }
            result.append(d)

        return web.json_response({
            "success": True,
            "events": result,
            "total": len(result),
        })
    except Exception as e:
        logger.error(f"Failed to list events: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def create_event(request: web.Request) -> web.Response:
    """Create an event (for external callers like the platform, tenant-isolated)."""
    tenant_id = request.get("tenant_id")
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)

    event_type = payload.get("event_type")
    data = payload.get("data", "{}")
    trace_id = payload.get("trace_id", "")
    metadata = payload.get("metadata", "{}")

    if not event_type:
        return web.json_response({"success": False, "error": "event_type is required"}, status=400)

    try:
        repo = EventRepository()
        data_str = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)
        metadata_str = json.dumps(metadata, ensure_ascii=False) if isinstance(metadata, dict) else str(metadata)
        event_id = await run_in_db_thread(lambda: repo.create_event(event_type, data_str, trace_id, metadata_str, tenant_id=tenant_id))

        # Broadcast to subscribers
        ws_server = request.app.get("ws_server")
        if ws_server:
            await ws_server._broadcast_event(event_type, {
                **(data if isinstance(data, dict) else {"raw": str(data)}),
                "tenant_id": tenant_id,
            })

        return web.json_response({"success": True, "event_id": event_id})
    except Exception as e:
        logger.error(f"Failed to create event: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


def register_event_routes(app: web.Application) -> None:
    app.router.add_get("/api/events", list_events)
    app.router.add_post("/api/events", create_event)