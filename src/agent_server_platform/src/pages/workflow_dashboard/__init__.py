# Workflow Dashboard page — view, publish, and execute DAG workflows
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

        current_workflow_id = gr.State(value="")

        with gr.Column(visible=True) as zone_list:
            gr.Markdown("## Workflow DAG Management")
            with gr.Row():
                wf_state_filter = gr.Dropdown(
                    choices=["all", "active", "draft", "archived"],
                    value="all", label="State Filter", scale=1)
                wf_search = gr.Textbox(label="Search", placeholder="Workflow name...", scale=2)
                publish_btn = gr.Button("Publish from Scenario", variant="primary", scale=1)

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
            exec_param_info = gr.Markdown("")
            exec_schema = gr.State({})
            exec_params = gr.Textbox(
                label="Input Parameters (JSON)",
                lines=10, placeholder='{\n  "key": "value"\n}')
            exec_server_status = gr.Markdown("")
            with gr.Row():
                exec_submit = gr.Button("Execute", variant="primary")
                exec_back = gr.Button("Back")
            exec_result = gr.Markdown("")

            def _build_param_info(schema):
                props = schema.get("properties", {}) if isinstance(schema, dict) else {}
                required = set(schema.get("required", [])) if isinstance(schema, dict) else set()
                if not props:
                    return "*No input parameters required.*"
                lines = ["| Parameter | Type | Required | Description |",
                         "|-----------|------|----------|-------------|"]
                for key, prop in props.items():
                    ptype = prop.get("type", "string")
                    desc = prop.get("description", "")
                    example = prop.get("example", "")
                    req = "Yes" if key in required else "No"
                    example_str = f" e.g. `{example}`" if example != "" else ""
                    lines.append(f"| `{key}` | {ptype} | {req} | {desc}{example_str} |")
                return "\n".join(lines)

            def _build_defaults(schema):
                props = schema.get("properties", {}) if isinstance(schema, dict) else {}
                defaults = {}
                for key, prop in props.items():
                    defaults[key] = prop.get("example", "")
                return defaults

            def _coerce_params(params, schema):
                if not isinstance(params, dict):
                    try:
                        params = json.loads(params) if isinstance(params, str) else {}
                    except (json.JSONDecodeError, TypeError):
                        return None, "Invalid parameters format."
                props = schema.get("properties", {}) if isinstance(schema, dict) else {}
                for key, prop in props.items():
                    if key not in params:
                        continue
                    val = params[key]
                    expected = prop.get("type", "string")
                    if isinstance(val, str):
                        if expected == "integer":
                            try:
                                params[key] = int(val)
                            except ValueError:
                                return None, f"Parameter '{key}' must be an integer, got '{val}'"
                        elif expected == "number":
                            try:
                                params[key] = float(val)
                            except ValueError:
                                return None, f"Parameter '{key}' must be a number, got '{val}'"
                        elif expected == "boolean":
                            params[key] = val.lower() in ("true", "1", "yes")
                return params, ""

            def _show_execute_page(wf_id):
                resp = api_client.get_workflow(wf_id)
                if not resp.get("success"):
                    return (gr.update(visible=True), gr.update(visible=False),
                            f"Error: {resp.get('error')}", "", {}, "{}", "", "", "")

                wf = resp.get("workflow", {})
                schema = wf.get("input_schema", {})
                info_md = _build_param_info(schema)
                defaults = _build_defaults(schema)

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
                        info, info_md, schema, json.dumps(defaults, indent=2, ensure_ascii=False),
                        server_status, wf_id, "")

            show_execute_trigger.change(
                _show_execute_page,
                inputs=[show_execute_trigger],
                outputs=[zone_list, zone_execute, exec_info, exec_param_info,
                         exec_schema, exec_params, exec_server_status,
                         current_workflow_id, exec_result])

            def _do_execute(wf_id, params, schema):
                if not wf_id:
                    return "No workflow selected."
                params, err = _coerce_params(params, schema)
                if err:
                    return err
                required = (schema.get("required", [])
                            if isinstance(schema, dict) else [])
                for field in required:
                    if field not in params or params[field] == "":
                        return f"Missing required parameter: **{field}**"
                resp = api_client.execute_workflow(wf_id, params)
                if resp.get("success"):
                    exe = resp.get("execution", {})
                    return (f"**Execution started!** Scenario ID: `{exe.get('scenario_id', '')}` | "
                            f"Steps: {exe.get('total_steps', 0)}\n\n"
                            f"Track progress in the Scenarios dashboard.")
                return f"**Error:** {resp.get('error', 'Unknown')}"

            exec_submit.click(_do_execute,
                              inputs=[current_workflow_id, exec_params, exec_schema],
                              outputs=exec_result)
            exec_back.click(lambda: (gr.update(visible=True), gr.update(visible=False)),
                            outputs=[zone_list, zone_execute])

        gr.Timer(value=5, render=False)

    return page
