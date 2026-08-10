# Execution Servers page - 通过 REST API 查询远程 WebSocket Server 的执行服务器信息
import json
import gradio as gr
from core.websocket_api_client import list_servers, get_server, delete_server, cleanup_offline


# `source` -> UI label. Centralized so the list + detail stay consistent.
_SOURCE_LABELS = {
    "execution_server": "执行服务器",
    "human_agent": "人工 Agent",
}


def _source_label(source):
    """Map a registry `source` to a human label; unknown/empty -> '-'."""
    if not source:
        return "-"
    return _SOURCE_LABELS.get(source, source)


def _parse_env(env_info):
    """Parse the env_info blob into (host_dict, commands_dict).

    Defensive against both the current nested shape ({commands, host}) and
    a legacy flat {cmd: bool} blob, so the page never crashes on old rows.
    """
    if not env_info:
        return {}, {}
    try:
        env = json.loads(env_info) if isinstance(env_info, str) else env_info
    except (json.JSONDecodeError, TypeError):
        return {}, {}

    if isinstance(env, dict) and "commands" in env and "host" in env:
        return env.get("host", {}) or {}, env.get("commands", {}) or {}
    # legacy flat shape: treat the whole thing as commands
    if isinstance(env, dict):
        return {}, env
    return {}, {}


def _command_summary(commands):
    """Compact 'bash✓ python✓ codex✗ ...' string."""
    if not commands:
        return "-"
    return "  ".join(f"{k}{'✓' if v else '✗'}" for k, v in commands.items())


def _load_servers():
    """Return server rows for the list dataframe + summary counts."""
    try:
        response = list_servers()
        if not response.get("success"):
            return [], f"❌ 加载失败: {response.get('error', '未知错误')}"

        servers = response.get("servers", [])
    except Exception as e:
        return [], f"❌ 加载失败: {e}"

    rows = []
    connected = 0
    running = 0
    for s in servers:
        if s.get("connected"):
            connected += 1
        if s.get("status") == "running":
            running += 1

        host, commands = _parse_env(s.get("env_info", {}))
        last_hb = s.get("last_heartbeat")
        hb = str(last_hb)[5:19] if last_hb else "-"

        rows.append([
            s.get("server_id", ""),
            s.get("name", "-"),
            _source_label(s.get("source")),
            s.get("status", "unknown"),
            "✅ 在线" if s.get("connected") else "❌ 离线",
            f"{s.get('running_count', 0)}/{s.get('total_quota', 0)}",
            host.get("hostname", "-"),
            host.get("ip", "-"),
            host.get("os", "-"),
            _command_summary(commands),
            hb,
        ])

    offline = len(rows) - connected
    summary = (f"**共 {len(rows)} 个服务器** · 在线 {connected} · "
               f"离线 {offline} · 运行中 {running}")
    return rows, summary


def _server_detail(server_id):
    """Return full server record as a dict for the JSON detail view."""
    if not server_id:
        return {}

    try:
        response = get_server(server_id)
        if not response.get("success"):
            return {"error": response.get("error", f"未找到服务器: {server_id}")}

        s = response.get("server", {})
    except Exception as e:
        return {"error": str(e)}

    host, commands = _parse_env(s.get("env_info", {}))
    source = s.get("source")

    return {
        "server_id": s.get("server_id"),
        "name": s.get("name"),
        "source": source,
        "source_label": _source_label(source),
        "status": s.get("status"),
        "connected": bool(s.get("connected")),
        "total_quota": s.get("total_quota"),
        "running_count": s.get("running_count"),
        "host": host,
        "commands": commands,
        "last_heartbeat": s.get("last_heartbeat"),
        "updated_at": s.get("updated_at"),
    }


def _remove_server(server_id):
    """Remove a single offline server row from the registry.

    Connected servers are refused: deleting their row would leave the live WS
    connection (still in WSDispatcher.connections) invisible in the UI while
    it keeps receiving tasks - and the next heartbeat/status frame would
    silently no-op against the missing row. The server must disconnect first
    (or go stale and be swept offline) before it can be cleaned up.

    Returns (ok, message).
    """
    if not server_id:
        return False, "未选择服务器"

    try:
        # 先检查服务器状态
        detail_response = get_server(server_id)
        if not detail_response.get("success"):
            return False, f"未找到服务器: {server_id}"

        server = detail_response.get("server", {})
        if server.get("connected"):
            return False, f"服务器 {server_id} 在线，无法移除（请先断开其连接）"

        # 调用远程 API 删除服务器
        delete_response = delete_server(server_id)
        if delete_response.get("success"):
            return True, f"已移除服务器 {server_id}"
        else:
            return False, delete_response.get("error", "删除失败")

    except Exception as e:
        return False, f"删除失败: {e}"


def _cleanup_offline_servers():
    """Delete every disconnected server row (historical / useless entries).

    Safe for servers that will reconnect - they re-upsert on next register.
    Returns (count_deleted, message).
    """
    try:
        response = cleanup_offline()
        if not response.get("success"):
            return 0, f"❌ 清理失败: {response.get('error', '未知错误')}"

        n = response.get("deleted_count", 0)
        if n == 0:
            return 0, "没有可清理的离线服务器"
        return n, f"已清理 {n} 个离线服务器"
    except Exception as e:
        return 0, f"❌ 清理失败: {e}"


def create_page(global_state_component):
    """Create the execution-servers page."""
    with gr.Blocks() as page:
        gr.Markdown("# 执行 Agent 服务器 (Execution Servers)")
        gr.Markdown("*通过 WebSocket 接入的远程执行 Agent 服务器与人工 Agent，状态由心跳实时同步*")

        # ZONE A: list
        with gr.Column(elem_id="server_list_content") as server_list_content:
            summary_md = gr.Markdown("")
            with gr.Row():
                refresh_btn = gr.Button("🔄 刷新", variant="secondary")
                cleanup_btn = gr.Button("🧹 清理离线服务器", variant="stop")
            list_status_md = gr.Markdown("")

            server_list = gr.Dataframe(
                headers=["服务器ID", "名称", "来源", "状态", "连接", "运行/配额",
                         "宿主机", "IP", "操作系统", "命令支持", "最近心跳"],
                label="服务器列表",
                wrap=True,
                interactive=False,
            )

            show_detail_trigger = gr.Textbox(value="", visible=False)

        # ZONE B: detail
        with gr.Column(elem_id="server_detail_content", visible=False) as server_detail_content:
            gr.Markdown("## 服务器详情")
            server_id_display = gr.Textbox(label="服务器ID", interactive=False)
            server_detail = gr.JSON(label="详细信息")
            remove_status_md = gr.Markdown("")

            with gr.Row():
                back_btn = gr.Button("返回列表", variant="secondary")
                refresh_detail_btn = gr.Button("刷新", variant="primary")
                remove_btn = gr.Button("🗑️ 移除服务器", variant="stop",
                                       interactive=False)

        # Auto-refresh every 5s (status/heartbeat change live)
        refresh_timer = gr.Timer(value=5, render=False)

        def _load_both():
            rows, summary = _load_servers()
            return rows, summary

        def _show_detail_view(server_id):
            if not server_id:
                return (gr.update(), gr.update(), "", {}, gr.update(), "")
            detail = _server_detail(server_id)
            connected = bool(detail.get("connected"))
            if connected:
                remove_update = gr.update(interactive=False)
                remove_hint = "⚠️ 服务器在线，需先断开连接才能移除"
            else:
                remove_update = gr.update(interactive=True)
                remove_hint = ""
            return (gr.update(visible=False), gr.update(visible=True),
                    server_id, detail, remove_update, remove_hint)

        def _hide_detail():
            return (gr.update(visible=True), gr.update(visible=False),
                    "", {}, "")

        def _handle_select(evt: gr.SelectData):
            # Only react to clicks on the first column (server_id)
            if evt.index[1] == 0:
                return evt.value
            return None

        def _remove_handler(server_id):
            ok, msg = _remove_server(server_id)
            if ok:
                # back to list + reload + surface result in list status
                rows, summary = _load_servers()
                return (gr.update(visible=True), gr.update(visible=False),
                        "", {}, "", gr.update(interactive=False),
                        rows, summary, msg)
            # failure: stay in detail, show reason under the detail JSON
            return (gr.update(), gr.update(), gr.update(), gr.update(),
                    gr.update(), gr.update(), msg,
                    gr.update(), gr.update(), gr.update())

        def _cleanup_handler():
            _n, msg = _cleanup_offline_servers()
            rows, summary = _load_servers()
            return rows, summary, msg

        # wiring
        refresh_timer.tick(_load_both, outputs=[server_list, summary_md])
        refresh_btn.click(_load_both, outputs=[server_list, summary_md])
        cleanup_btn.click(
            _cleanup_handler,
            outputs=[server_list, summary_md, list_status_md],
        )
        server_list.select(fn=_handle_select, outputs=[show_detail_trigger])
        show_detail_trigger.change(
            _show_detail_view,
            inputs=[show_detail_trigger],
            outputs=[server_list_content, server_detail_content,
                     server_id_display, server_detail,
                     remove_btn, remove_status_md],
        )
        back_btn.click(
            _hide_detail,
            outputs=[server_list_content, server_detail_content,
                     show_detail_trigger, server_detail, remove_status_md],
        )
        refresh_detail_btn.click(
            _server_detail,
            inputs=[server_id_display],
            outputs=[server_detail, server_id_display],
        )
        remove_btn.click(
            _remove_handler,
            inputs=[server_id_display],
            outputs=[server_list_content, server_detail_content,
                     show_detail_trigger, server_detail, server_id_display,
                     remove_btn, remove_status_md,
                     server_list, summary_md, list_status_md],
        )

        page.load(_load_both, outputs=[server_list, summary_md])

    return page
