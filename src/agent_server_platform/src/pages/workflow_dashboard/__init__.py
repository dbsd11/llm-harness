# Workflow Dashboard page — view, publish, execute, and monitor DAG workflows
import json
import gradio as gr

from api_client import api_client


def _build_mermaid_dag(dag_def):
    steps = dag_def.get("steps", []) if isinstance(dag_def, dict) else []
    if not steps:
        return "graph TD\n    A[No steps]"
    lines = ["graph TD"]
    for step in steps:
        sid = step.get("step_id", "")
        label = (step.get("goal_template", "") or "")[:40]
        lines.append(f"    {sid}[\"{sid}: {label}\"]")
    for step in steps:
        sid = step.get("step_id", "")
        for dep in step.get("depends_on", []):
            lines.append(f"    {dep} --> {sid}")
    return "\n".join(lines)


def _build_run_mermaid_dag(dag_def, task_states):
    steps = dag_def.get("steps", []) if isinstance(dag_def, dict) else []
    if not steps:
        return "graph TD\n    A[No steps]"
    state_colors = {
        "success": "#d4edda",
        "failed": "#f8d7da",
        "running": "#fff3cd",
        "pending": "#e2e3e5",
    }
    lines = ["graph TD"]
    for step in steps:
        sid = step.get("step_id", "")
        label = (step.get("goal_template", "") or "")[:40]
        st = task_states.get(sid, "pending")
        color = state_colors.get(st, "#e2e3e5")
        lines.append(f'    {sid}["{sid}: {label}"]\n    style {sid} fill:{color}')
    for step in steps:
        sid = step.get("step_id", "")
        for dep in step.get("depends_on", []):
            lines.append(f"    {dep} --> {sid}")
    return "\n".join(lines)


def create_page(global_state_component):
    with gr.Blocks(css="""
        .wf-action-btn {
            min-width: 0px !important; width: auto !important;
            max-width: 60px !important; padding: 2px 6px !important;
            font-size: 11px !important; border-radius: 3px !important;
        }
        .wf-param-row { margin-bottom: 4px; }
    """) as page:
        show_detail_trigger = gr.Textbox(value="", visible=False)
        show_execute_trigger = gr.Textbox(value="", visible=False)
        show_publish_trigger = gr.Textbox(value="", visible=False)
        show_monitor_trigger = gr.Textbox(value="", visible=False)
        show_runs_trigger = gr.Textbox(value="", visible=False)
        show_run_detail_trigger = gr.Textbox(value="", visible=False)

        current_workflow_id = gr.State(value="")
        current_execution_id = gr.State(value="")

        with gr.Column(visible=True) as zone_list:
            gr.Markdown("## Workflow DAG Management")
            with gr.Row():
                wf_state_filter = gr.Dropdown(
                    choices=["all", "active", "draft", "archived"],
                    value="all", label="State Filter", scale=1)
                wf_search = gr.Textbox(label="Search", placeholder="Workflow name...", scale=2)
                publish_btn = gr.Button("Publish from Scenario", variant="primary", scale=1)
                runs_btn = gr.Button("Recent Runs", variant="secondary", scale=1)

            @gr.render(inputs=[wf_state_filter, wf_search, show_publish_trigger])
            def render_workflow_list(state_filter, search, _publish_trigger):
                resp = api_client.list_workflows(
                    state=state_filter if state_filter != "all" else None)
                workflows = resp.get("workflows", []) if isinstance(resp, dict) else []
                if search:
                    workflows = [w for w in workflows
                                 if search.lower() in (w.get("name", "") or "").lower()]

                if not workflows:
                    gr.Markdown("*No workflows found.*")
                    return

                for wf in workflows:
                    with gr.Row():
                        gr.Markdown(
                            f"**{wf.get('name', 'Unnamed')}** | "
                            f"v{wf.get('version', 1)} | "
                            f"State: `{wf.get('state', '?')}` | "
                            f"Steps: {len(wf.get('dag_definition', {}).get('steps', []))} | "
                            f"Source: `{(wf.get('source_scenario_id', '') or '')[:12]}...`")
                        detail_btn = gr.Button("Detail", size="sm", elem_classes="wf-action-btn")
                        execute_btn = gr.Button("Execute", size="sm",
                                                elem_classes="wf-action-btn", variant="primary")
                        wf_state = wf.get("state", "active")
                        if wf_state == "active":
                            toggle_btn = gr.Button("Archive", size="sm",
                                                   elem_classes="wf-action-btn")
                        else:
                            toggle_btn = gr.Button("Activate", size="sm",
                                                   elem_classes="wf-action-btn", variant="secondary")
                        delete_btn = gr.Button("Delete", size="sm",
                                               elem_classes="wf-action-btn", variant="stop")

                        wid = wf.get("workflow_id", "")

                        def _show_detail(wid=wid):
                            return wid

                        def _show_execute(wid=wid):
                            return wid

                        detail_btn.click(_show_detail, outputs=show_detail_trigger)
                        execute_btn.click(_show_execute, outputs=show_execute_trigger)

                        def _toggle_state(wid=wid, current_state=wf_state):
                            new_state = "archived" if current_state == "active" else "active"
                            api_client.update_workflow(wid, {"state": new_state})

                        def _delete(wid=wid):
                            api_client.delete_workflow(wid)

                        toggle_btn.click(_toggle_state)
                        delete_btn.click(_delete)

        with gr.Column(visible=False) as zone_publish:
            gr.Markdown("## Publish Scenario as Workflow")
            gr.Markdown("Select a completed scenario to extract its execution DAG into a reusable workflow.")
            pub_scenario_id = gr.Dropdown(label="Completed Scenario ID", choices=[],
                                          allow_custom_value=True, scale=3)
            pub_name = gr.Textbox(label="Workflow Name", placeholder="My Workflow")
            pub_desc = gr.Textbox(label="Description", placeholder="Optional description")
            with gr.Row():
                pub_submit = gr.Button("Publish", variant="primary")
                pub_back = gr.Button("Back to List")
            pub_result = gr.Markdown("")

            def _load_scenarios():
                resp = api_client.list_scenarios(state="completed")
                scenarios = resp.get("scenarios", []) if isinstance(resp, dict) else []
                choices = [(f"{s.get('name', '')} ({s.get('scenario_id', '')[:12]}...)",
                            s.get("scenario_id", "")) for s in scenarios]
                return gr.update(choices=choices)

            publish_btn.click(lambda: (gr.update(visible=False), gr.update(visible=True)),
                              outputs=[zone_list, zone_publish]).then(
                _load_scenarios, outputs=pub_scenario_id)

            def _do_publish(scenario_id, name, desc):
                if not scenario_id:
                    return "Please select a scenario.", gr.update(), gr.update()
                resp = api_client.publish_workflow(scenario_id, name, desc)
                if resp.get("success"):
                    wf = resp.get("workflow", {})
                    return (
                        f"**Published!** Workflow ID: `{wf.get('workflow_id', '')}` | "
                        f"Steps: {wf.get('step_count', 0)} | Version: {wf.get('version', 1)}",
                        gr.update(visible=True), gr.update(visible=False))
                return f"**Error:** {resp.get('error', 'Unknown')}", gr.update(), gr.update()

            pub_submit.click(_do_publish,
                             inputs=[pub_scenario_id, pub_name, pub_desc],
                             outputs=[pub_result, zone_list, zone_publish])
            pub_back.click(lambda: (gr.update(visible=True), gr.update(visible=False)),
                           outputs=[zone_list, zone_publish])

        with gr.Column(visible=False) as zone_detail:
            gr.Markdown("## Workflow Detail")
            detail_content = gr.Markdown("")
            detail_dag = gr.HTML("")
            detail_templates = gr.Dataframe(
                headers=["Step", "Goal Template", "Agent Role", "Server", "Depends On",
                         "Experience"],
                label="Task Templates", wrap=True)
            detail_schema = gr.JSON(label="Input Schema")
            detail_back = gr.Button("Back to List")

            def _show_detail_page(wf_id):
                resp = api_client.get_workflow(wf_id)
                if not resp.get("success"):
                    return (gr.update(visible=True), gr.update(visible=False),
                            f"Error: {resp.get('error')}", "", [], {}, "")

                wf = resp.get("workflow", {})
                templates = wf.get("templates", [])

                dag_html = f'<div style="border:1px solid #ddd;padding:12px;border-radius:6px;"><pre style="font-size:12px;overflow-x:auto;">{_build_mermaid_dag(wf.get("dag_definition", {}))}</pre></div>'

                tmpl_rows = []
                for t in templates:
                    exp = t.get("experience_note", {})
                    exp_str = f"{exp.get('previous_state', '?')} ({exp.get('tips', '')})"
                    tmpl_rows.append([
                        t.get("step_id", ""),
                        t.get("goal_template", "")[:80],
                        t.get("agent_role", ""),
                        t.get("server_id", "") or "(auto)",
                        ", ".join(t.get("depends_on", [])),
                        exp_str,
                    ])

                info = (
                    f"**{wf.get('name', '')}** | Version: {wf.get('version', 1)} | "
                    f"State: `{wf.get('state', '')}`\n\n"
                    f"{wf.get('description', '')}\n\n"
                    f"Source Scenario: `{wf.get('source_scenario_id', '')}`"
                )

                return (gr.update(visible=False), gr.update(visible=True),
                        info, dag_html, tmpl_rows, wf.get("input_schema", {}), wf_id)

            show_detail_trigger.change(
                _show_detail_page,
                inputs=[show_detail_trigger],
                outputs=[zone_list, zone_detail, detail_content, detail_dag,
                         detail_templates, detail_schema, current_workflow_id])

            detail_back.click(lambda: (gr.update(visible=True), gr.update(visible=False)),
                              outputs=[zone_list, zone_detail])

        with gr.Column(visible=False) as zone_execute:
            gr.Markdown("## Execute Workflow")
            exec_info = gr.Markdown("")
            exec_params = gr.JSON(label="Input Parameters", value={})
            exec_server_status = gr.Markdown("")
            with gr.Row():
                exec_submit = gr.Button("Execute", variant="primary")
                exec_back = gr.Button("Back")
            exec_result = gr.Markdown("")

            def _show_execute_page(wf_id):
                resp = api_client.get_workflow(wf_id)
                if not resp.get("success"):
                    return (gr.update(visible=True), gr.update(visible=False),
                            f"Error: {resp.get('error')}", {}, "", "", "")

                wf = resp.get("workflow", {})
                schema = wf.get("input_schema", {})
                default_params = {}
                for key, prop in schema.get("properties", {}).items():
                    default_params[key] = prop.get("example", "")

                servers_resp = api_client.list_servers()
                servers = servers_resp.get("servers", []) if isinstance(servers_resp, dict) else []
                online_ids = {s.get("server_id") for s in servers if s.get("connected")}

                templates = wf.get("templates", [])
                required_servers = set()
                for t in templates:
                    sid = t.get("server_id", "")
                    if sid:
                        required_servers.add(sid)

                status_parts = []
                for sid in required_servers:
                    if sid in online_ids:
                        status_parts.append(f"  {sid}: online")
                    else:
                        status_parts.append(f"  {sid}: **OFFLINE**")

                server_status = "Server Status:\n" + "\n".join(status_parts) if status_parts else "No specific servers required."

                info = f"**{wf.get('name', '')}** (v{wf.get('version', 1)})"
                return (gr.update(visible=False), gr.update(visible=True),
                        info, default_params, server_status, wf_id, "")

            show_execute_trigger.change(
                _show_execute_page,
                inputs=[show_execute_trigger],
                outputs=[zone_list, zone_execute, exec_info, exec_params,
                         exec_server_status, current_workflow_id, exec_result])

            def _do_execute(wf_id, params):
                if not wf_id:
                    return "No workflow selected."
                if not isinstance(params, dict):
                    try:
                        params = json.loads(params) if isinstance(params, str) else {}
                    except (json.JSONDecodeError, TypeError):
                        return "Invalid parameters format."
                resp = api_client.execute_workflow(wf_id, params)
                if resp.get("success"):
                    exe = resp.get("execution", {})
                    return (f"**Execution started!** ID: `{exe.get('execution_id', '')}` | "
                            f"Steps: {exe.get('total_steps', 0)}")
                return f"**Error:** {resp.get('error', 'Unknown')}"

            exec_submit.click(_do_execute,
                              inputs=[current_workflow_id, exec_params],
                              outputs=exec_result)
            exec_back.click(lambda: (gr.update(visible=True), gr.update(visible=False)),
                            outputs=[zone_list, zone_execute])

        with gr.Column(visible=False) as zone_runs:
            gr.Markdown("## Recent Workflow Runs")
            with gr.Row():
                run_state_filter = gr.Dropdown(
                    choices=["all", "running", "completed", "failed", "cancelled"],
                    value="all", label="State Filter", scale=1)
                runs_back = gr.Button("Back to List", scale=1)

            @gr.render(inputs=[run_state_filter, show_runs_trigger])
            def render_run_list(state_filter, _trigger):
                resp = api_client.list_all_workflow_executions(
                    limit=50,
                    state=state_filter if state_filter != "all" else None)
                executions = resp.get("executions", []) if isinstance(resp, dict) else []

                wf_map = {}
                wf_resp = api_client.list_workflows()
                for w in (wf_resp.get("workflows", []) if isinstance(wf_resp, dict) else []):
                    wf_map[w.get("workflow_id", "")] = w.get("name", "Unknown")

                if not executions:
                    gr.Markdown("*No executions found.*")
                    return

                for exe in executions:
                    with gr.Row():
                        eid = exe.get("execution_id", "")
                        wf_name = wf_map.get(exe.get("workflow_id", ""), "Unknown")
                        state = exe.get("state", "?")
                        completed = exe.get("completed_steps", 0)
                        total = exe.get("total_steps", 0)
                        started = (exe.get("started_at") or "")[:19]
                        gr.Markdown(
                            f"`{eid[:12]}...` | **{wf_name}** | "
                            f"State: `{state}` | Steps: {completed}/{total} | "
                            f"Started: {started}")
                        run_detail_btn = gr.Button(
                            "Detail", size="sm", elem_classes="wf-action-btn")

                        def _show_run_detail(eid=eid):
                            return eid

                        run_detail_btn.click(
                            _show_run_detail, outputs=show_run_detail_trigger)

            runs_btn.click(
                lambda: (gr.update(visible=False), gr.update(visible=True)),
                outputs=[zone_list, zone_runs])
            runs_back.click(
                lambda: (gr.update(visible=True), gr.update(visible=False)),
                outputs=[zone_list, zone_runs])

        with gr.Column(visible=False) as zone_run_detail:
            gr.Markdown("## Execution Run Detail")
            run_exec_info = gr.JSON(label="Execution Info")
            run_task_table = gr.Dataframe(
                headers=["Task ID", "Goal", "State", "Agent", "Duration(s)", "Error"],
                label="Task Steps", wrap=True)
            run_dag_html = gr.HTML("")
            run_msg_table = gr.Dataframe(
                headers=["Time", "Type", "Sender", "Receiver", "Content"],
                label="Message History", wrap=True)
            with gr.Row():
                run_detail_back = gr.Button("Back to Runs")
                run_cancel_btn = gr.Button("Cancel Execution", variant="stop")
            run_detail_timer = gr.Timer(value=5, render=False)

            def _navigate_to_run_detail(exec_id):
                if not exec_id:
                    return gr.update(), gr.update(), ""
                return (gr.update(visible=False), gr.update(visible=True),
                        exec_id)

            def _refresh_run_detail(exec_id):
                if not exec_id:
                    return {}, [], "", []

                resp = api_client.get_workflow_execution_detail(exec_id)
                if not resp.get("success"):
                    return {"error": resp.get("error", "Not found")}, [], "", []

                exe = resp.get("execution", {})
                tasks = exe.get("tasks", [])

                task_rows = []
                task_states = {}
                for t in tasks:
                    state = t.get("state", "")
                    goal = (t.get("goal", "") or "")[:80]
                    task_states[goal.split(":")[0]] = state
                    task_rows.append([
                        t.get("task_id", "")[:16] + "...",
                        goal,
                        state,
                        t.get("agent_name", "") or t.get("agent_role", "") or "",
                        t.get("execution_duration", "") or "",
                        (t.get("error", "") or "")[:80],
                    ])

                wf_resp = api_client.get_workflow(exe.get("workflow_id", ""))
                dag_def = {}
                if wf_resp.get("success"):
                    dag_def = wf_resp.get("workflow", {}).get("dag_definition", {})

                for t in tasks:
                    goal = (t.get("goal", "") or "")[:80]
                    sid = goal.split(":")[0]
                    task_states[sid] = t.get("state", "pending")

                dag_text = _build_run_mermaid_dag(dag_def, task_states)
                dag_html = (
                    f'<div style="border:1px solid #ddd;padding:12px;'
                    f'border-radius:6px;">'
                    f'<pre style="font-size:12px;overflow-x:auto;">'
                    f'{dag_text}</pre></div>'
                )

                msg_rows = []
                scenario_id = exe.get("scenario_id", "")
                if scenario_id:
                    msg_resp = api_client.get_scenario_messages(scenario_id)
                    entries = (msg_resp.get("messages", [])
                               if msg_resp.get("success") else [])
                    msg_rows = [
                        [e.get("time", ""), e.get("type", ""),
                         e.get("sender", ""), e.get("receiver", ""),
                         e.get("content", "")]
                        for e in entries
                    ]
                    msg_rows.sort(key=lambda r: r[0], reverse=True)
                else:
                    task_results = exe.get("task_results", {})
                    if isinstance(task_results, dict):
                        for step_id, step_data in task_results.items():
                            result = step_data.get("result", {})
                            state = step_data.get("state", "")
                            question = (result.get("question", "") or "")[:120]
                            output = (result.get("output", "") or "")[:200]
                            created = exe.get("created_at", "")
                            completed = exe.get("completed_at", "")
                            msg_rows.append([
                                created, "dispatch", "scheduling",
                                "execution",
                                f"[{step_id}] {question}",
                            ])
                            msg_rows.append([
                                completed, "reply", "execution",
                                "scheduling",
                                f"[{step_id}] {state}: {output}",
                            ])

                return exe, task_rows, dag_html, msg_rows

            show_run_detail_trigger.change(
                _navigate_to_run_detail,
                inputs=[show_run_detail_trigger],
                outputs=[zone_runs, zone_run_detail, current_execution_id]).then(
                _refresh_run_detail,
                inputs=[current_execution_id],
                outputs=[run_exec_info, run_task_table, run_dag_html,
                         run_msg_table])

            run_detail_timer.tick(
                _refresh_run_detail,
                inputs=[current_execution_id],
                outputs=[run_exec_info, run_task_table, run_dag_html,
                         run_msg_table])

            run_detail_back.click(
                lambda: (gr.update(visible=True), gr.update(visible=False), ""),
                outputs=[zone_runs, zone_run_detail, current_execution_id])

            def _cancel_run(exec_id):
                if exec_id:
                    api_client.cancel_workflow_execution(exec_id)
                return (gr.update(visible=True), gr.update(visible=False), "")

            run_cancel_btn.click(
                _cancel_run,
                inputs=[current_execution_id],
                outputs=[zone_runs, zone_run_detail, current_execution_id])

        gr.Timer(value=5, render=False)

    return page
