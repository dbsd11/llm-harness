# Agent Registry page — queries ws_server API directly
import json
import gradio as gr

from api_client import api_client


def create_page(global_state_component):
    """Create agent registry page — reads agent data from ws_server API."""
    with gr.Blocks() as page:
        gr.Markdown("# Agent 注册表 (Agent Registry)")
        gr.Markdown("*场景运行时自动注册的底层 Agent，场景结束后自动清理*")

        with gr.Column(elem_id="agent_list_content") as agent_list_content:
            with gr.Row():
                with gr.Column(scale=3):
                    with gr.Row():
                        type_filter = gr.Dropdown(
                            choices=["all", "scheduling", "execution"],
                            label="Agent 类型", value="all")
                        status_filter = gr.Dropdown(
                            choices=["all", "active", "inactive", "error"],
                            label="状态", value="all")
                with gr.Column(scale=1):
                    refresh_btn = gr.Button("刷新", variant="secondary")

            agent_list = gr.Dataframe(
                headers=["Agent ID", "场景ID", "类型", "名称", "角色/配置", "状态", "创建时间"],
                label="运行中的 Agent", wrap=True)

            show_detail_trigger = gr.Textbox(value="", visible=False)

        with gr.Column(elem_id="agent_detail_content", visible=False) as agent_detail_content:
            gr.Markdown("## Agent 详情")
            agent_id_display = gr.Textbox(label="Agent ID", interactive=False)
            agent_detail = gr.JSON(label="Agent 详细信息")
            with gr.Row():
                back_btn = gr.Button("返回列表", variant="secondary")
                refresh_detail_btn = gr.Button("刷新", variant="primary")

        refresh_timer = gr.Timer(value=5, render=False)

        def load_agents(type_val, status_val):
            resp = api_client.list_agents(
                agent_type=type_val if type_val != "all" else None,
                status=status_val if status_val != "all" else None)
            agents = resp.get("agents", [])
            rows = []
            for a in agents:
                config_summary = ""
                cfg = a.get("config") or {}
                if cfg:
                    role = cfg.get("role", "")
                    sys_prompt = cfg.get("system_prompt", "")
                    if role:
                        config_summary = f"角色: {role}"
                    if sys_prompt:
                        config_summary += f" | {sys_prompt[:50]}"
                    if not config_summary:
                        config_summary = str(cfg)[:80]

                scenario_display = "—"
                if a.get("scenario_id"):
                    sc_id = a["scenario_id"]
                    scenario_display = sc_id[:12] + "..." if len(sc_id) > 12 else sc_id

                rows.append([
                    a.get("agent_id", ""),
                    scenario_display,
                    a.get("agent_type", ""),
                    a.get("name") or "—",
                    config_summary,
                    a.get("status", ""),
                    str(a.get("created_at") or "—"),
                ])
            return rows

        def show_agent_detail(agent_id):
            if not agent_id: return {}, ""
            resp = api_client.get_agent(agent_id)
            if not resp.get("success"): return {"error": "Not found"}, ""
            a = resp["agent"]
            return a, agent_id

        def show_detail_view(agent_id):
            if agent_id:
                detail, did = show_agent_detail(agent_id)
                return gr.update(visible=False), gr.update(visible=True), did, detail
            return gr.update(), gr.update(), "", {}

        def hide_detail():
            return gr.update(visible=True), gr.update(visible=False), "", {}

        def handle_agent_select(evt: gr.SelectData):
            if evt.index[1] == 0:
                return evt.value
            return None

        refresh_timer.tick(load_agents, inputs=[type_filter, status_filter], outputs=[agent_list])
        refresh_btn.click(load_agents, inputs=[type_filter, status_filter], outputs=[agent_list])
        agent_list.select(fn=handle_agent_select, outputs=[show_detail_trigger])
        show_detail_trigger.change(show_detail_view, inputs=[show_detail_trigger],
            outputs=[agent_list_content, agent_detail_content, agent_id_display, agent_detail])
        back_btn.click(hide_detail,
            outputs=[agent_list_content, agent_detail_content, show_detail_trigger, agent_detail])
        refresh_detail_btn.click(show_agent_detail, inputs=[agent_id_display],
            outputs=[agent_detail, agent_id_display])
        page.load(load_agents, inputs=[type_filter, status_filter], outputs=[agent_list])

    return page