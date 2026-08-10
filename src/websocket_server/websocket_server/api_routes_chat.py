"""Scene Assistant chatbot REST API for ws_server (aiohttp)."""

import json
from datetime import datetime
from aiohttp import web

from logger import logger

# Import assistant functions (lazy — only loaded when chat API is called)
from services.scene_assistant import (
    reply, render_preview, save_scene, parse_scene_spec,
    load_scenario_spec, gather_scene_index, SceneAssistant,
)
from services.compression import should_compress, build_summary_prompt
from database.repositories.assistant_message_repository import AssistantMessageRepository
from core.llm_client import llm_client
# 专用 DB / LLM 线程池：把同步 DB I/O 与同步 LLM 调用移出 aiohttp event loop，
# 避免一次 /api/chat（LLM 可达数十~上百秒）夯死整个 WS 服务。
from common.utils.global_loop_util import run_in_db_thread, run_in_llm_thread


async def chat(request: web.Request) -> web.Response:
    """Handle a chat turn for the scene management assistant.

    Request: { "session_id": "...", "message": "...", "history": [...],
               "scene_id": "optional-scene-id" }
    Response: { "reply": "...", "preview": "...", "scene_spec": {...} or null,
                "session_id": "..." }
    """
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)

    user_text = payload.get("message", "")
    session_id = payload.get("session_id", "default")
    history = payload.get("history", [])
    saved_id = payload.get("scene_id")
    pending_scene = payload.get("pending_scene")

    if not user_text.strip():
        return web.json_response({"success": False, "error": "message is required"}, status=400)

    try:
        # Save user message (DB I/O off the loop)
        msg_repo = AssistantMessageRepository()
        await run_in_db_thread(msg_repo.save, "user", user_text, session_id)

        # Check if compression is needed
        msg_count = await run_in_db_thread(msg_repo.count_messages, session_id)
        summary_content = None
        if should_compress(msg_count):
            from services.compression import COMPRESS_BATCH
            old_msgs = await run_in_db_thread(
                lambda: msg_repo.find_old_messages(session_id, limit=COMPRESS_BATCH)
            )
            if old_msgs:
                summary_prompt = build_summary_prompt(old_msgs)
                # LLM 摘要调用走专用线程池，绝不阻塞 event loop
                llm_summary = await run_in_llm_thread(
                    llm_client.chat,
                    [{"role": "user", "content": summary_prompt}], 0.3,
                )
                if llm_summary:
                    await run_in_db_thread(msg_repo.save, "summary", llm_summary, session_id)
                    ids_to_delete = [m.id for m in old_msgs]
                    await run_in_db_thread(msg_repo.delete_messages_by_ids, ids_to_delete)
                    summary_obj = await run_in_db_thread(msg_repo.find_latest_summary, session_id)
                    if summary_obj:
                        summary_content = summary_obj.content

        # Call assistant logic (reply 内部含同步 LLM 调用，整体放 LLM 线程池)
        visible, new_pending, preview_md, new_saved_id = await run_in_llm_thread(
            reply, user_text, history, pending_scene, saved_id, summary_content
        )

        # Save assistant reply
        await run_in_db_thread(msg_repo.save, "assistant", visible, session_id)

        # Parse scene spec from reply
        spec = parse_scene_spec(visible)

        return web.json_response({
            "success": True,
            "reply": visible,
            "preview": preview_md,
            "scene_spec": spec,
            "pending_scene": new_pending,
            "scene_id": new_saved_id,
            "session_id": session_id,
        })
    except Exception as e:
        logger.error(f"Chat error: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def chat_save(request: web.Request) -> web.Response:
    """Persist a scene-spec draft to database.

    Request: { "session_id": "...", "scene_spec": {...}, "scene_id": "optional-update-id" }
    """
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)

    spec = payload.get("scene_spec") or payload.get("pending_scene")
    session_id = payload.get("session_id", "default")
    scene_id = payload.get("scene_id")

    if not spec or not isinstance(spec, dict):
        return web.json_response({"success": False, "error": "scene_spec is required"}, status=400)

    try:
        saved_id, error = await run_in_db_thread(save_scene, spec, scene_id)
        if error:
            return web.json_response({"success": False, "error": error}, status=400)

        # Broadcast event
        ws_server = request.app.get("ws_server")
        if ws_server:
            await ws_server._broadcast_event("scenario_created" if not scene_id else "scenario_updated", {
                "scenario_id": saved_id,
                "name": spec.get("name", ""),
            })

        # Save confirmation message
        msg_repo = AssistantMessageRepository()
        action = "更新" if scene_id else "创建"
        await run_in_db_thread(
            msg_repo.save, "assistant",
            f"✅ 场景已{action}保存（ID: {saved_id[:8]}...）", session_id,
        )

        return web.json_response({
            "success": True,
            "scenario_id": saved_id,
            "message": f"场景已{'更新' if scene_id else '创建'}",
        })
    except Exception as e:
        logger.error(f"Chat save error: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def chat_preview(request: web.Request) -> web.Response:
    """Get current scene-spec preview for a session."""
    session_id = request.query.get("session_id", "default")
    spec = request.query.get("spec")
    if spec:
        try:
            spec_obj = json.loads(spec)
        except Exception:
            spec_obj = None
    else:
        spec_obj = None

    preview_md = render_preview(spec_obj)
    return web.json_response({"success": True, "preview": preview_md})


async def chat_load_scene(request: web.Request) -> web.Response:
    """Load an existing scenario's spec for editing."""
    scene_id = request.query.get("scene_id")
    if not scene_id:
        return web.json_response({"success": False, "error": "scene_id is required"}, status=400)

    spec = await run_in_db_thread(load_scenario_spec, scene_id)
    if not spec:
        return web.json_response({"success": False, "error": "Scene not found"}, status=404)

    return web.json_response({"success": True, "scene_spec": spec})


async def chat_scene_index(request: web.Request) -> web.Response:
    """Get scene index for the assistant."""
    index = await run_in_db_thread(gather_scene_index)
    return web.json_response({"success": True, "scene_index": index})


def register_chat_routes(app: web.Application) -> None:
    app.router.add_post("/api/chat", chat)
    app.router.add_post("/api/chat/save", chat_save)
    app.router.add_get("/api/chat/preview", chat_preview)
    app.router.add_get("/api/chat/load-scene", chat_load_scene)
    app.router.add_get("/api/chat/scene-index", chat_scene_index)