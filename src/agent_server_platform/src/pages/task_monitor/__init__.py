# Task Monitor page — thin presentation layer via ws_server API
import random
import gradio as gr

from api_client import api_client


def create_page(global_state_component):
    """Create task monitor page — API-driven via ws_server."""
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
        gr.Markdown("# Task Monitor")
        gr.Markdown("*Monitor task execution and state transitions*")

        refresh_trigger = gr.Textbox(value="", visible=False)
        show_accept_trigger = gr.Textbox(value="", visible=False)

        # ZONE A: Task list
        with gr.Column(elem_id="task_list_content") as task_list_content:
            with gr.Row():
                with gr.Column(scale=3):
                    state_filter = gr.Dropdown(
                        choices=["all", "pending", "running", "waiting",
                                 "pending_review", "success", "failed", "timeout", "cancelled"],
                        label="Filter by State", value="all")
                with gr.Column(scale=1):
                    refresh_btn = gr.Button("Refresh", variant="secondary")

            @gr.render(inputs=[state_filter, refresh_trigger])
            def render_task_list(state="all", trigger=""):
                # Fetch tasks from ws_server API
                resp = api_client.list_tasks(state=state if state != "all" else None)
                tasks = resp.get("tasks", [])

                # Build scenario name map
                scenario_names = {}
                sc_ids = {t.get("scenario_id") for t in tasks if t.get("scenario_id")}
                if sc_ids:
                    sc_resp = api_client.list_scenarios()
                    for s in sc_resp.get("scenarios", []):
                        scenario_names[s.get("scenario_id", "")] = s.get("name", "")

                with gr.Row():
                    gr.Markdown("**Task ID**"), gr.Markdown("**Scenario**"), gr.Markdown("**Goal**")
                    gr.Markdown("**State**"), gr.Markdown("**Agent**"), gr.Markdown("**Duration**")
                    gr.Markdown("**操作**")

                for task in tasks:
                    tid = task.get("task_id", "")
                    sc_id = task.get("scenario_id", "")
                    sc_display = sc_id[:12] + "..." if len(sc_id) > 12 else (sc_id or "—")
                    sc_name = scenario_names.get(sc_id, "—")
                    goal_disp = (task.get("goal", "") or "")[:50]
                    dur = task.get("execution_duration")
                    dur_disp = f"{dur:.2f}" if dur is not None else ""

                    with gr.Row():
                        gr.Markdown(tid)
                        gr.Markdown(f"{sc_display}\n{sc_name}")
                        gr.Markdown(goal_disp)
                        gr.Markdown(task.get("state", ""))
                        gr.Markdown(task.get("agent_name") or "—")
                        gr.Markdown(dur_disp)

                        with gr.Column(scale=1, min_width=50):
                            with gr.Row(elem_classes=["action-row"]):
                                if task.get("state") == "pending_review":
                                    _h = gr.Textbox(value=tid, visible=False)
                                    review_btn = gr.Button("✅", size="sm", variant="primary",
                                        elem_classes=["action-btn-small"])
                                    review_btn.click(fn=lambda t=tid: t, inputs=[_h], outputs=[show_accept_trigger])

        # ZONE B: Acceptance review
        with gr.Column(elem_id="task_accept_content", visible=False) as task_accept_content:
            gr.Markdown("## 任务验收")
            accept_task_id = gr.Textbox(label="Task ID", interactive=False)
            accept_task_info = gr.Markdown("")
            accept_decision = gr.Radio(choices=["通过", "不通过"], label="验收决定", value="通过")
            accept_feedback = gr.Textbox(lines=3, label="补充反馈（不通过时必填）", placeholder="请输入反馈信息...")
            accept_error_msg = gr.Markdown("")
            with gr.Row():
                accept_confirm_btn = gr.Button("确认提交", variant="primary")
                accept_cancel_btn = gr.Button("返回列表", variant="secondary")

        refresh_timer = gr.Timer(value=5, render=False)

        def _build_task_info(task_id):
            resp = api_client.get_task(task_id)
            if not resp.get("success"):
                return "", task_id
            t = resp["task"]
            result_str = ""
            if t.get("result"):
                import json
                try:
                    result_str = json.loads(str(t["result"])).get("output", "")[:500]
                except Exception:
                    result_str = str(t["result"])[:500]
            info = f"**Task ID**: {t.get('task_id')}\n\n"
            info += f"**Goal**: {t.get('goal')}\n\n"
            info += f"**Agent 输出**:\n{result_str}\n\n"
            info += f"**状态**: {t.get('state')}"
            return info, task_id

        def open_accept_view(task_id):
            if not task_id:
                return gr.update(visible=True), gr.update(visible=False), "", "", "通过", "", ""
            info, tid = _build_task_info(task_id)
            if not tid:
                return gr.update(visible=True), gr.update(visible=False), "", "", "通过", "", "❌ 任务未找到"
            return gr.update(visible=False), gr.update(visible=True), tid, info, "通过", "", ""

        def close_accept_view():
            return gr.update(visible=True), gr.update(visible=False), "", "", "通过", "", ""

        def do_accept(task_id, decision, feedback):
            if not task_id:
                return gr.update(visible=True), gr.update(visible=False), "", "", "通过", "", "❌ 未选择任务"

            passed = (decision == "通过")
            if not passed and not (feedback and feedback.strip()):
                return gr.update(visible=False), gr.update(visible=True), task_id, \
                       _build_task_info(task_id)[0], decision, feedback, "❌ 不通过时必须填写补充反馈"

            resp = api_client.accept_task(task_id, passed=passed, feedback=feedback.strip() if feedback else "")
            if resp.get("success"):
                return gr.update(visible=True), gr.update(visible=False), "", "", "通过", "", \
                       f"✅ 验收{'通过' if passed else '不通过'}: {task_id}"
            return gr.update(visible=False), gr.update(visible=True), task_id, \
                   _build_task_info(task_id)[0], decision, feedback, resp.get("error", "验收失败")

        # Wiring
        show_accept_trigger.change(fn=open_accept_view, inputs=[show_accept_trigger],
            outputs=[task_list_content, task_accept_content,
                     accept_task_id, accept_task_info, accept_decision, accept_feedback, accept_error_msg])
        accept_confirm_btn.click(fn=do_accept, inputs=[accept_task_id, accept_decision, accept_feedback],
            outputs=[task_list_content, task_accept_content,
                     accept_task_id, accept_task_info, accept_decision, accept_feedback, accept_error_msg])
        accept_cancel_btn.click(fn=close_accept_view,
            outputs=[task_list_content, task_accept_content,
                     accept_task_id, accept_task_info, accept_decision, accept_feedback, accept_error_msg])

        refresh_btn.click(fn=lambda: str(random.randint(1, 100000)), outputs=[refresh_trigger])
        refresh_timer.tick(fn=lambda: str(random.randint(1, 100000)), outputs=[refresh_trigger])

    return page