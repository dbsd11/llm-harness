"""Tool registry REST API for ws_server (aiohttp) -- hot-swap support."""

from aiohttp import web

from core.agents.tool_registry import tool_registry
from logger import logger


async def list_tools(request: web.Request) -> web.Response:
    """List all registered tools."""
    try:
        tools = tool_registry.list_tools()
        return web.json_response({
            "success": True,
            "tools": tools,
            "total": len(tools),
        })
    except Exception as e:
        logger.error(f"Failed to list tools: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def get_tool(request: web.Request) -> web.Response:
    """Get details for a specific tool."""
    tool_name = request.match_info["tool_name"]
    try:
        info = tool_registry.get_tool_info(tool_name)
        return web.json_response({"success": True, "tool": info})
    except ValueError:
        return web.json_response(
            {"success": False, "error": f"Tool not found: {tool_name}"}, status=404)
    except Exception as e:
        logger.error(f"Failed to get tool {tool_name}: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def delete_tool(request: web.Request) -> web.Response:
    """Unregister a tool (hot-swap). Requires authentication."""
    tool_name = request.match_info["tool_name"]
    tenant_id = request.get("tenant_id")
    if not tenant_id:
        return web.json_response({"success": False, "error": "tenant_id required"}, status=401)
    try:
        removed = tool_registry.unregister(tool_name)
        if not removed:
            return web.json_response(
                {"success": False, "error": f"Tool not found: {tool_name}"}, status=404)
        return web.json_response({
            "success": True,
            "message": f"Tool '{tool_name}' unregistered",
        })
    except Exception as e:
        logger.error(f"Failed to unregister tool {tool_name}: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


def register_tool_routes(app: web.Application) -> None:
    app.router.add_get("/api/tools", list_tools)
    app.router.add_get("/api/tools/{tool_name}", get_tool)
    app.router.add_delete("/api/tools/{tool_name}", delete_tool)
