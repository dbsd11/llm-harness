# Scenario Dashboard page — thin presentation layer via ws_server API
import json
import random
import gradio as gr

from api_client import api_client
from pages.scenario_dashboard.export_html import build_message_history, write_export_file

# ponytail: inline registry, add new scenarios here
SCENARIO_REGISTRY = {
    "simple_qa": "simple_qa",
    "code_execution": "code_execution",
}

STARTABLE_STATES = {"initializing", "cancelled"}


def server_choices():
    """Dropdown choices for execution-server selector via ws_server API."""
    choices = [("本地（后端执行）", "")]
    try:
        resp = api_client.list_servers()
        servers = resp.get("servers", []) if isinstance(resp, dict) else []
        for s in servers:
            if not isinstance(s, dict):
                continue
            sid = s.get("server_id", "")
            name = s.get("name", sid)
            source = s.get("source", "")
            if source == "human_agent":
                choices.append((f"👤 {name} ({sid})", sid))
            else:
                choices.append((f"{name} ({sid})", sid))
    except Exception:
        pass
    return choices


def start_guard(status):
    if not status:
        return True, "❌ 场景未找到"
    state = status.get("state")
    if state in ("running", "starting", "awaiting_review"):
        return True, f"⏳ 场景正在运行中（{state}），无需重复启动"
    if state in ("completed", "failed"):
        return True, f"🔒 场景已结束（{state}），无法重启"
    if state not in STARTABLE_STATES:
        return True, f"❌ 当前状态（{state}）不支持启动"
    return False, ""


SCENARIO_CONFIG_FIELDS = {
    "simple_qa": [
        {"name": "question", "label": "问题 (Question)", "type": "text", "required": True, "placeholder": "What is 2+2?"},
        {"name": "timeout", "label": "超时时间 (秒)", "type": "number", "required": False, "value": 60},
    ],
    "code_execution": [
        {"name": "script", "label": "Python 脚本", "type": "code", "required": False, "placeholder": "print('Hello World')"},
        {"name": "code", "label": "代码片段", "type": "text", "required": False, "placeholder": "print('Hello World')"},
        {"name": "timeout", "label": "超时时间 (秒)", "type": "number", "required": False, "value": 300},
    ],
}


def create_page(global_state_component):
    """Create scenario dashboard page — API-driven via ws_server."""
    with gr.Blocks(css="""
        .action-btn-small {
            min-width: 0px !important; width: auto !important;
            max-width: 30px !important; padding: 2px 4px !important;
            font-size: 10px !important; border-radius: 3px !important;
            flex-shrink: 1 !important; line-height: 1 !important;
        }
        .action-btn-small:hover { transform: translateY(-1px) !important; box-shadow: 0 2px 4px rgba(0,0,0,0.1) !important; }
        .action-row { display: flex !important; flex-wrap: nowrap !important; gap: 2px !important;
                      align-items: center !important; justify-content: flex-start !important; width: auto !important; }
    """) as page:
        show_create_trigger = gr.Textbox(value="", visible=False)
        show_action_trigger = gr.Textbox(value="", visible=False)
        show_detail_trigger = gr.Textbox(value="", visible=False)
        show_clone_trigger = gr.Textbox(value="", visible=False)
        refresh_trigger = gr.Textbox(value="", visible=False)

        # ZONE A: list
        with gr.Column(elem_id="scenario_list_content") as scenario_list_content:
            with gr.Row():
                with gr.Column(scale=3):
                    gr.Markdown("# 场景仪表板 (Scenario Dashboard)")
                    gr.Markdown("*创建和监控场景执行*")
                with gr.Column(scale=1):
                    new_scenario_btn = gr.Button("新建场景", variant="primary")

            with gr.Row():
                with gr.Column(scale=2):
                    search_input = gr.Textbox(placeholder="搜索场景名称...", label="场景名称搜索", value="")
                with gr.Column(scale=1):
                    state_filter = gr.Dropdown(
                        choices=["all", "initializing", "running", "awaiting_review", "completed", "failed", "cancelled"],
                        label="状态筛选", value="all")
                with gr.Column(scale=1):
                    type_filter = gr.Dropdown(
                        choices=["all"] + list(SCENARIO_REGISTRY.keys()),
                        label="类型筛选", value="all")
                with gr.Column(scale=1):
                    refresh_btn = gr.Button("刷新", variant="secondary")

            @gr.render(inputs=[search_input, state_filter, type_filter, refresh_trigger])
            def render_scenario_list(search_text="", state="all", stype="all", trigger=""):
                # Fetch scenarios from ws_server API
                resp = api_client.list_scenarios()
                scenarios = resp.get("scenarios", [])

                if search_text and search_text.strip():
                    s_lower = search_text.lower().strip()
                    scenarios = [s for s in scenarios if s_lower in s.get("name", "").lower()]
                if state != "all":
                    scenarios = [s for s in scenarios if s.get("state") == state]
                if stype != "all":
                    scenarios = [s for s in scenarios if s.get("scenario_type") == stype]

                scenarios.sort(key=lambda x: x.get("created_at", ""), reverse=True)

                with gr.Row():
                    gr.Markdown("**场景ID**"), gr.Markdown("**类型**"), gr.Markdown("**名称**")
                    gr.Markdown("**状态**"), gr.Markdown("**创建时间**"), gr.Markdown("**操作**")

                for scenario in scenarios:
                    sc_id = scenario["scenario_id"]
                    sc_state = scenario.get("state", "")
                    with gr.Row():
                        gr.Markdown(sc_id)
                        gr.Markdown(scenario.get("scenario_type", ""))
                        gr.Markdown(scenario.get("name", ""))
                        gr.Markdown(sc_state)
                        gr.Markdown(scenario.get("created_at", ""))
                        with gr.Column(scale=1, min_width=50):
                            id_text = gr.Textbox(value=sc_id, visible=False)
                            with gr.Row(elem_classes=["action-row"]):
                                detail_btn = gr.Button("👁️", size="sm", elem_classes=["action-btn-small"])
                                detail_btn.click(fn=lambda id: id, inputs=[id_text], outputs=[show_detail_trigger])
                                clone_btn = gr.Button("📋", size="sm", elem_classes=["action-btn-small"])
                                clone_btn.click(fn=lambda id: id, inputs=[id_text], outputs=[show_clone_trigger])
                                if sc_state in STARTABLE_STATES:
                                    start_btn = gr.Button("▶️", size="sm", variant="primary", elem_classes=["action-btn-small"])
                                    start_btn.click(fn=lambda id: f"{id}@start", inputs=[id_text], outputs=[show_action_trigger])
                                if sc_state in ("running", "starting", "awaiting_review"):
                                    stop_btn = gr.Button("⏹️", size="sm", variant="stop", elem_classes=["action-btn-small"])
                                    stop_btn.click(fn=lambda id: f"{id}@stop", inputs=[id_text], outputs=[show_action_trigger])

        # ZONE B: Create view
        with gr.Column(elem_id="scenario_create_content", visible=False) as scenario_create_content:
            gr.Markdown("## 创建新场景")
            with gr.Row():
                scenario_type = gr.Dropdown(choices=list(SCENARIO_REGISTRY.keys()), label="场景类型", value="simple_qa")
                scenario_name = gr.Textbox(label="场景名称", placeholder="我的场景")
                scenario_desc = gr.Textbox(label="场景描述", placeholder="场景描述")

            gr.Markdown("### Agent 角色配置")
            with gr.Column():
                gr.Markdown("**调度 Agent 角色**")
                scheduling_agent_role = gr.Textbox(
                    label="调度 Agent 角色定义",
                    placeholder="例如：你是一个任务调度专家...",
                    lines=3,
                    value="你是一个任务调度专家，负责分析用户目标，将其分解为可执行的子任务，并协调执行 Agent 完成任务。")

            with gr.Column():
                gr.Markdown("**执行 Agent 角色**（可配置多个，最多 10 个）")
                execution_roles_count = gr.State(value=1)
                role_inputs = []
                for i in range(10):
                    with gr.Group(visible=(i == 0)) as role_group:
                        name_input = gr.Textbox(label=f"角色 {i+1} 名称", value="代码执行专家" if i == 0 else "")
                        role_input = gr.Textbox(label=f"角色 {i+1} 定义",
                            value="你是一个代码执行专家，负责执行代码并返回结果。" if i == 0 else "", lines=2)
                        server_input = gr.Dropdown(label=f"角色 {i+1} 执行服务器", choices=server_choices(), value="")
                        role_inputs.append({"group": role_group, "name": name_input, "role": role_input, "server": server_input})
                with gr.Row():
                    add_role_btn = gr.Button("➕ 添加执行 Agent 角色", variant="secondary", size="sm")
                    remove_role_btn = gr.Button("➖ 删除最后一个角色", variant="stop", size="sm")

            gr.Markdown("### 场景配置")
            with gr.Column(visible=True) as simple_qa_config:
                qa_question = gr.Textbox(label="问题 (Question)", placeholder="What is 2+2?", lines=2)
                qa_timeout = gr.Number(label="超时时间 (秒)", value=60, minimum=1)
            with gr.Column(visible=False) as code_execution_config:
                code_script = gr.Code(label="Python 脚本", language="python", lines=10)
                code_code = gr.Textbox(label="代码片段", placeholder="print('Hello World')", lines=3)
                code_timeout = gr.Number(label="超时时间 (秒)", value=300, minimum=1)

            gr.Markdown("### 验收设置")
            manual_acceptance_box = gr.Checkbox(label="需要人工验收", value=False,
                info="启用后每个任务执行完成后需要人工审核")

            gr.Markdown("### 关联资源库（可选）")
            gr.Markdown("配置场景关联的 Git 资源库，调度 Agent 执行时可从中获取代码上下文。私有仓库需填写用户名和 Token。")
            resource_repo_count = gr.State(value=0)
            repo_inputs = []
            for i in range(5):
                with gr.Group(visible=(i == 0)) as repo_group:
                    url_input = gr.Textbox(label=f"资源库 {i+1} Git URL",
                                          placeholder="https://github.com/org/repo.git")
                    desc_input = gr.Textbox(label=f"资源库 {i+1} 说明",
                                           placeholder="核心业务代码")
                    user_input = gr.Textbox(label=f"资源库 {i+1} 用户名（私有仓库）",
                                           placeholder="可选", visible=True)
                    token_input = gr.Textbox(label=f"资源库 {i+1} Token/密码（私有仓库）",
                                            placeholder="可选", type="password")
                    repo_inputs.append({"group": repo_group, "url": url_input, "desc": desc_input,
                                       "user": user_input, "token": token_input})
            with gr.Row():
                add_repo_btn = gr.Button("➕ 添加资源库", variant="secondary", size="sm")
                remove_repo_btn = gr.Button("➖ 删除最后一个资源库", variant="stop", size="sm")

            status_msg = gr.Markdown("")
            with gr.Row():
                create_btn = gr.Button("创建场景", variant="primary")
                cancel_create_btn = gr.Button("取消", variant="secondary")

        # ZONE C: Actions
        with gr.Column(elem_id="scenario_action_content", visible=False) as scenario_action_content:
            gr.Markdown("## 场景操作")
            action_scenario_id = gr.Textbox(label="场景ID", interactive=False)
            action_status_msg = gr.Markdown("")
            with gr.Row():
                start_btn = gr.Button("启动", variant="primary")
                stop_btn = gr.Button("停止", variant="stop")
                back_to_list_btn = gr.Button("返回列表", variant="secondary")

        # ZONE D: Detail
        with gr.Column(elem_id="scenario_detail_content", visible=False) as scenario_detail_content:
            gr.Markdown("## 场景详情")
            detail_id_input = gr.Textbox(label="场景ID", interactive=False)
            scenario_detail = gr.JSON(label="场景信息")
            message_history = gr.Dataframe(
                headers=["时间", "类型", "发送方", "接收方", "内容"],
                label="Agent 通信历史记录", wrap=True)
            with gr.Row():
                back_from_detail_btn = gr.Button("返回列表", variant="secondary")
                refresh_detail_btn = gr.Button("刷新", variant="primary")
                export_html_btn = gr.DownloadButton("📄 导出 HTML", variant="secondary")

            detail_refresh_timer = gr.Timer(value=5, render=False)
            server_refresh_timer = gr.Timer(value=10, render=False)

        # ── Helper functions ───────────────────────────────────────

        def add_execution_role(current_count):
            if current_count is None: current_count = 0
            new_count = min(current_count + 1, 10)
            return [new_count] + [gr.update(visible=(i < new_count)) for i in range(10)]

        def remove_execution_role(current_count):
            if current_count is None or current_count <= 1: current_count = 1
            new_count = max(current_count - 1, 1)
            return [new_count] + [gr.update(visible=(i < new_count)) for i in range(10)]

        def add_resource_repo(current_count):
            if current_count is None: current_count = 0
            new_count = min(current_count + 1, 5)
            return [new_count] + [gr.update(visible=(i < new_count)) for i in range(5)]

        def remove_resource_repo(current_count):
            if current_count is None or current_count <= 0: current_count = 0
            new_count = max(current_count - 1, 0)
            return [new_count] + [gr.update(visible=(i < new_count)) for i in range(5)]

        def toggle_config_fields(stype):
            return (gr.update(visible=(stype == "simple_qa")),
                    gr.update(visible=(stype == "code_execution")))

        def create_scenario(stype, sname, sdesc, sched_role, role_count,
                           r0n,r0r,r1n,r1r,r2n,r2r,r3n,r3r,r4n,r4r,
                           r5n,r5r,r6n,r6r,r7n,r7r,r8n,r8r,r9n,r9r,
                           r0s,r1s,r2s,r3s,r4s,r5s,r6s,r7s,r8s,r9s,
                           qa_q, qa_t, code_s, code_c, code_t, manual_acc,
                           repo_count,
                           rp0u,rp0d,rp0user,rp0token,rp1u,rp1d,rp1user,rp1token,
                           rp2u,rp2d,rp2user,rp2token,rp3u,rp3d,rp3user,rp3token,
                           rp4u,rp4d,rp4user,rp4token):
            noop = tuple(gr.update() for _ in range(41))
            try:
                agent_roles = {"scheduling_agent": {"role": sched_role or "任务调度专家"},
                               "execution_agents": []}
                role_vals = [r0n,r0r,r1n,r1r,r2n,r2r,r3n,r3r,r4n,r4r,
                            r5n,r5r,r6n,r6r,r7n,r7r,r8n,r8r,r9n,r9r]
                server_vals = [r0s,r1s,r2s,r3s,r4s,r5s,r6s,r7s,r8s,r9s]
                if role_count and role_count > 0:
                    for i in range(min(role_count, 10)):
                        name = role_vals[i * 2] if i * 2 < len(role_vals) else ""
                        role = role_vals[i * 2 + 1] if i * 2 + 1 < len(role_vals) else ""
                        if name and role:
                            entry = {"name": name, "role": role}
                            sid = server_vals[i] if i < len(server_vals) else ""
                            if sid: entry["server_id"] = sid
                            agent_roles["execution_agents"].append(entry)

                config = {"agent_roles": agent_roles}
                if stype == "simple_qa":
                    if not qa_q: return ("❌ 错误: 问题不能为空",) + noop
                    config.update({"question": qa_q, "timeout": int(qa_t) if qa_t else 60})
                elif stype == "code_execution":
                    if not code_s and not code_c:
                        return ("❌ 错误: 脚本或代码不能为空",) + noop
                    config.update({"script": code_s or "", "code": code_c or "",
                                   "timeout": int(code_t) if code_t else 300})
                config["manual_acceptance"] = bool(manual_acc)

                # Collect resource repos
                repo_url_vals = [rp0u, rp1u, rp2u, rp3u, rp4u]
                repo_desc_vals = [rp0d, rp1d, rp2d, rp3d, rp4d]
                repo_user_vals = [rp0user, rp1user, rp2user, rp3user, rp4user]
                repo_token_vals = [rp0token, rp1token, rp2token, rp3token, rp4token]
                resource_repos = []
                for i in range(min(repo_count or 0, 5)):
                    url = repo_url_vals[i] if i < len(repo_url_vals) else ""
                    desc = repo_desc_vals[i] if i < len(repo_desc_vals) else ""
                    user = repo_user_vals[i] if i < len(repo_user_vals) else ""
                    token = repo_token_vals[i] if i < len(repo_token_vals) else ""
                    if url and url.strip():
                        repo_data = {"git_url": url.strip(), "description": desc.strip() or ""}
                        if user and user.strip():
                            repo_data["username"] = user.strip()
                        if token and token.strip():
                            repo_data["token"] = token.strip()
                        resource_repos.append(repo_data)
                if resource_repos:
                    config["resource_repos"] = resource_repos

                resp = api_client.create_scenario(stype, sname, sdesc, config)
                if resp.get("success"):
                    sc_id = resp["scenario"].get("scenario_id", "")
                    return (f"✅ 已创建: {sc_id}",) + noop
                return (f"❌ 错误: {resp.get('error', 'Unknown')}",) + noop
            except Exception as e:
                return (f"❌ 错误: {str(e)}",) + noop

        def show_create_view():
            server_upd = gr.update(choices=server_choices())
            return (gr.update(visible=False), gr.update(visible=True),
                    gr.update(visible=False), gr.update(visible=False),
                    *[server_upd for _ in range(10)])

        def show_clone_view(scenario_id):
            # Fetch scenario from ws_server API and pre-fill form
            if not scenario_id:
                return (gr.update(), gr.update(), gr.update(), gr.update(), "") + \
                       tuple([gr.update()]*5) + tuple([gr.update()]*46) + (gr.update(),) + \
                       tuple([gr.update()]*5) + tuple([gr.update()]*20) + (gr.update(),)

            resp = api_client.get_scenario(scenario_id)
            if not resp.get("success"):
                return (gr.update(visible=False), gr.update(visible=True), gr.update(visible=False),
                        gr.update(visible=False), "", gr.update(), gr.update(), gr.update(),
                        gr.update(), gr.update()) + tuple([gr.update()]*10) + tuple([gr.update()]*20) + \
                        tuple([gr.update()]*10) + ("", 60, "", "", 300, False, 0) + \
                        tuple([gr.update()]*5) + tuple([gr.update()]*20) + \
                        (f"❌ 未找到: {scenario_id}",)

            s = resp["scenario"]
            config = s.get("config", {})
            agent_roles = config.get("agent_roles", {}) or {}
            sched = agent_roles.get("scheduling_agent", {}).get("role", "")
            exec_agents = agent_roles.get("execution_agents", []) or []
            stype = s.get("scenario_type", "simple_qa")

            count = max(1, min(len(exec_agents), 10))
            role_upds = []
            server_upds = []
            choices = server_choices()
            for i in range(10):
                if i < len(exec_agents):
                    role_upds.append(exec_agents[i].get("name", ""))
                    role_upds.append(exec_agents[i].get("role", ""))
                    sid = exec_agents[i].get("server_id", "") or ""
                    server_upds.append(gr.update(choices=choices, value=sid))
                else:
                    role_upds.extend(["", ""])
                    server_upds.append(gr.update(choices=choices, value=""))

            # Extract resource repos
            repos = config.get("resource_repos") or []
            repo_count = min(len(repos), 5)
            repo_upds = []
            for i in range(5):
                if i < len(repos):
                    repo_upds.append(repos[i].get("git_url", ""))
                    repo_upds.append(repos[i].get("description", ""))
                    repo_upds.append(repos[i].get("username", ""))
                    repo_upds.append(repos[i].get("token", ""))
                else:
                    repo_upds.extend(["", "", "", ""])

            role_group_upds = [gr.update(visible=(i < count)) for i in range(10)]
            repo_group_upds = [gr.update(visible=(i < repo_count)) for i in range(5)]

            return (gr.update(visible=False), gr.update(visible=True), gr.update(visible=False),
                    gr.update(visible=False), "", stype, f"{s.get('name', '')} (克隆)",
                    s.get("description", ""), sched, count) + \
                   tuple(role_group_upds) + tuple(role_upds) + tuple(server_upds) + (
                       config.get("question", ""), config.get("timeout", 60),
                       config.get("script", ""), config.get("code", ""), config.get("timeout", 300),
                       config.get("manual_acceptance", False), repo_count) + \
                   tuple(repo_group_upds) + tuple(repo_upds) + (f"📋 已从场景 `{scenario_id[:8]}` 克隆配置",)

        def hide_create_view():
            role_vals = ["代码执行专家", "你是一个代码执行专家，负责执行代码并返回结果。"]
            for _ in range(9):
                role_vals.extend(["", ""])
            return (gr.update(visible=True), gr.update(visible=False), gr.update(visible=False),
                    gr.update(visible=False), "", "simple_qa", "", "",
                    "你是一个任务调度专家...", 1) + \
                   tuple(role_vals) + tuple([""] * 10) + ("", 60, "", "", 300, False, 0,
                       "", "", "", "",  # repo1: url, desc, user, token
                       "", "", "", "",  # repo2
                       "", "", "", "",  # repo3
                       "", "", "", "",  # repo4
                       "", "", "", "",  # repo5
                       "")  # status_msg

        def show_action_view(trigger_value):
            if trigger_value:
                parts = trigger_value.split("@") if "@" in trigger_value else (trigger_value, "")
                sc_id, action = parts[0], parts[1] if len(parts) > 1 else ""
                status = {}
                resp = api_client.get_scenario(sc_id)
                if resp.get("success"):
                    status = resp["scenario"]

                status_text = f"**当前状态**: {status.get('state', '未知')}"
                result_msg = ""
                if action == "start" and status:
                    blocked, msg = start_guard(status)
                    if blocked:
                        result_msg = msg
                    else:
                        start_resp = api_client.start_scenario(sc_id)
                        result_msg = f"✅ 已启动: {sc_id}" if start_resp.get("success") else f"❌ 启动失败: {start_resp.get('error', '')}"
                elif action == "stop":
                    stop_resp = api_client.stop_scenario(sc_id)
                    result_msg = f"✅ 已停止: {sc_id}" if stop_resp.get("success") else f"❌ 停止失败: {stop_resp.get('error', '')}"

                return (gr.update(visible=False), gr.update(visible=False),
                        gr.update(visible=True), gr.update(visible=False),
                        sc_id, status_text + ("\n\n" + result_msg if result_msg else ""))
            return gr.update(), gr.update(), gr.update(), gr.update(), "", ""

        def hide_action_view():
            return gr.update(visible=True), gr.update(visible=False), gr.update(visible=False), \
                   gr.update(visible=False), "", ""

        def show_scenario_detail(sc_id):
            if not sc_id: return {}, [], "", gr.update(visible=False)
            resp = api_client.get_scenario(sc_id)
            if not resp.get("success"):
                return {"error": "Not found"}, [], "", gr.update(visible=False)

            status = resp["scenario"]

            # Fetch message history from ws_server (same data as before refactoring)
            msg_resp = api_client.get_scenario_messages(sc_id)
            entries = msg_resp.get("messages", []) if msg_resp.get("success") else []

            rows = [[e.get("time", ""), e.get("type", ""), e.get("sender", ""),
                     e.get("receiver", ""), e.get("content", "")]
                    for e in entries]
            rows.sort(key=lambda r: r[0], reverse=True)

            export_visible = status.get("state") in ("completed", "failed", "cancelled")
            return status, rows, sc_id, gr.update(visible=export_visible)

        def show_detail_view(sc_id):
            if sc_id:
                detail, history, detail_id, export_upd = show_scenario_detail(sc_id)
                return (gr.update(visible=False), gr.update(visible=False),
                        gr.update(visible=False), gr.update(visible=True),
                        detail_id, detail, history, export_upd)
            return gr.update(), gr.update(), gr.update(), gr.update(), "", {}, [], gr.update(visible=False)

        def hide_detail_view():
            return gr.update(visible=True), gr.update(visible=False), gr.update(visible=False), \
                   gr.update(visible=False), "", {}, [], gr.update(visible=False)

        # ── Wiring ────────────────────────────────────────────────

        refresh_btn.click(fn=lambda: str(random.randint(1, 10000)), outputs=[refresh_trigger])
        new_scenario_btn.click(show_create_view, outputs=[scenario_list_content, scenario_create_content,
            scenario_action_content, scenario_detail_content] + [r["server"] for r in role_inputs])
        scenario_type.change(fn=toggle_config_fields, inputs=[scenario_type],
            outputs=[simple_qa_config, code_execution_config])

        create_btn.click(fn=create_scenario,
            inputs=[scenario_type, scenario_name, scenario_desc, scheduling_agent_role, execution_roles_count] +
            [item for r in role_inputs for item in [r["name"], r["role"]]] +
            [r["server"] for r in role_inputs] +
            [qa_question, qa_timeout, code_script, code_code, code_timeout, manual_acceptance_box,
             resource_repo_count] +
            [item for r in repo_inputs for item in [r["url"], r["desc"], r["user"], r["token"]]],
            outputs=[status_msg] + [item for r in role_inputs for item in [r["name"], r["role"]]]).then(
            fn=hide_create_view, outputs=[scenario_list_content, scenario_create_content,
                scenario_action_content, scenario_detail_content, show_create_trigger, scenario_type,
                scenario_name, scenario_desc, scheduling_agent_role, execution_roles_count] +
            [item for r in role_inputs for item in [r["name"], r["role"]]] +
            [r["server"] for r in role_inputs] + [qa_question, qa_timeout, code_script, code_code, code_timeout,
                manual_acceptance_box, resource_repo_count] +
            [item for r in repo_inputs for item in [r["url"], r["desc"], r["user"], r["token"]]] + [status_msg])

        cancel_create_btn.click(fn=hide_create_view, outputs=[scenario_list_content, scenario_create_content,
            scenario_action_content, scenario_detail_content, show_create_trigger, scenario_type,
            scenario_name, scenario_desc, scheduling_agent_role, execution_roles_count] +
            [item for r in role_inputs for item in [r["name"], r["role"]]] +
            [r["server"] for r in role_inputs] + [qa_question, qa_timeout, code_script, code_code, code_timeout,
                manual_acceptance_box, resource_repo_count] +
            [item for r in repo_inputs for item in [r["url"], r["desc"], r["user"], r["token"]]] + [status_msg])

        add_role_btn.click(fn=add_execution_role, inputs=[execution_roles_count],
            outputs=[execution_roles_count] + [r["group"] for r in role_inputs])
        remove_role_btn.click(fn=remove_execution_role, inputs=[execution_roles_count],
            outputs=[execution_roles_count] + [r["group"] for r in role_inputs])

        add_repo_btn.click(fn=add_resource_repo, inputs=[resource_repo_count],
            outputs=[resource_repo_count] + [r["group"] for r in repo_inputs])
        remove_repo_btn.click(fn=remove_resource_repo, inputs=[resource_repo_count],
            outputs=[resource_repo_count] + [r["group"] for r in repo_inputs])

        def execute_start(sc_id):
            if not sc_id: return "", "❌ 请输入场景ID"
            resp = api_client.get_scenario(sc_id)
            status = resp.get("scenario", {}) if resp.get("success") else {}
            blocked, msg = start_guard(status)
            if blocked: return sc_id, msg
            start_resp = api_client.start_scenario(sc_id)
            msg = f"✅ 已启动: {sc_id}" if start_resp.get("success") else f"❌ 启动失败: {start_resp.get('error', '')}"
            return sc_id, msg

        def execute_stop(sc_id):
            if not sc_id: return "", "❌ 请输入场景ID"
            resp = api_client.stop_scenario(sc_id)
            msg = f"✅ 已停止: {sc_id}" if resp.get("success") else f"❌ 停止失败: {resp.get('error', '')}"
            return sc_id, msg

        start_btn.click(fn=execute_start, inputs=[action_scenario_id], outputs=[action_scenario_id, action_status_msg])
        stop_btn.click(fn=execute_stop, inputs=[action_scenario_id], outputs=[action_scenario_id, action_status_msg])
        back_to_list_btn.click(fn=hide_action_view, outputs=[scenario_list_content, scenario_create_content,
            scenario_action_content, scenario_detail_content, show_action_trigger, action_status_msg])

        back_from_detail_btn.click(fn=hide_detail_view, outputs=[scenario_list_content, scenario_create_content,
            scenario_action_content, scenario_detail_content, show_detail_trigger, scenario_detail, message_history, export_html_btn])
        refresh_detail_btn.click(lambda sc_id: show_scenario_detail(sc_id), inputs=[detail_id_input],
            outputs=[scenario_detail, message_history, detail_id_input, export_html_btn])
        export_html_btn.click(fn=write_export_file, inputs=[detail_id_input], outputs=[export_html_btn])

        detail_refresh_timer.tick(lambda sc_id: show_scenario_detail(sc_id), inputs=[detail_id_input],
            outputs=[scenario_detail, message_history, detail_id_input, export_html_btn])

        def _refresh_server_choices(*current_values):
            choices = server_choices()
            valid = {v for _, v in choices}
            return [gr.update(choices=choices, value=v if v in valid else "") for v in current_values]

        server_refresh_timer.tick(fn=_refresh_server_choices,
            inputs=[r["server"] for r in role_inputs],
            outputs=[r["server"] for r in role_inputs])

        show_detail_trigger.change(fn=show_detail_view, inputs=[show_detail_trigger],
            outputs=[scenario_list_content, scenario_create_content, scenario_action_content, scenario_detail_content,
                     detail_id_input, scenario_detail, message_history, export_html_btn])
        show_action_trigger.change(fn=show_action_view, inputs=[show_action_trigger],
            outputs=[scenario_list_content, scenario_create_content, scenario_action_content, scenario_detail_content,
                     action_scenario_id, action_status_msg])
        show_clone_trigger.change(fn=show_clone_view, inputs=[show_clone_trigger],
            outputs=[scenario_list_content, scenario_create_content, scenario_action_content, scenario_detail_content,
                     show_create_trigger, scenario_type, scenario_name, scenario_desc,
                     scheduling_agent_role, execution_roles_count] +
                    [r["group"] for r in role_inputs] +
                    [item for r in role_inputs for item in [r["name"], r["role"]]] +
                    [r["server"] for r in role_inputs] +
                    [qa_question, qa_timeout, code_script, code_code, code_timeout, manual_acceptance_box,
                     resource_repo_count] + [r["group"] for r in repo_inputs] +
                    [item for r in repo_inputs for item in [r["url"], r["desc"], r["user"], r["token"]]] + [status_msg])

    return page