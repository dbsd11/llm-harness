# 个人任务列表页 — Gradio Timer 轮询本地 DB，替代 WebSocket 订阅
#
# HumanAgentClient 以执行 agent 相同的协议连接到远程 WS 服务器，收到 task 帧后保存到
# 本地 human_tasks 表。本页每 5 秒查询该表，将待处理任务渲染为 HTML Tab。提交结果通过
# HumanAgentClient 发送 task_result 帧回到 WS 服务器。
import os
import json
import gradio as gr

from database.repositories.human_task_repository import HumanTaskRepository
from core.human_agent_client import get_human_agent_client

# 复用的样式（与改造前一致）
_HUMAN_CSS = """
#ha-tabs { margin-top: 8px; }
.ha-empty { color: #64748b; padding: 16px; }
.ha-tabs { display: flex; flex-direction: column; gap: 8px; }
.ha-tab { border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden; }
.ha-tab-title {
  background: #f1f5f9; padding: 8px 12px; cursor: pointer;
  font-weight: 600; font-size: 13px; user-select: none;
}
.ha-tab-title:hover { background: #e2e8f0; }
.ha-tab-active .ha-tab-title { background: #3b82f6; color: #fff; }
.ha-tab-active .ha-tab-title:hover { background: #2563eb; }
.ha-tab-body { padding: 12px; display: block; }
.ha-tab:not(.ha-tab-active) .ha-tab-body { display: none; }
.ha-meta { color: #64748b; font-size: 12px; margin-bottom: 6px; }
.ha-goal { margin-bottom: 8px; }
.ha-ctx summary { cursor: pointer; color: #64748b; font-size: 12px; }
.ha-ctx pre { background: #f8fafc; padding: 8px; border-radius: 6px; font-size: 12px; overflow-x: auto; }
"""

# JS for tab click handling — stores active task_id in #ha-active-task hidden input
_TAB_JS = """
<script>
(function(){
  var container = document.getElementById('ha-tabs');
  if (!container) return;
  container.addEventListener('click', function(e){
    var title = e.target.closest('.ha-tab-title');
    if (!title) return;
    var tab = title.closest('.ha-tab');
    if (!tab) return;
    // deactivate all tabs first
    container.querySelectorAll('.ha-tab').forEach(function(t){ t.classList.remove('ha-tab-active'); });
    // activate clicked tab
    tab.classList.add('ha-tab-active');
    // store active task_id
    var taskId = tab.getAttribute('data-task') || '';
    var hidden = document.getElementById('ha-active-task');
    if (hidden) hidden.value = taskId;
  });
  // on load: set initial active task id
  var firstTab = container.querySelector('.ha-tab-active');
  if (firstTab) {
    var tid = firstTab.getAttribute('data-task') || '';
    var h = document.getElementById('ha-active-task');
    if (h) h.value = tid;
  }
})();
</script>
"""

def _escape(s):
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _truncate(s, n):
    s = str(s or "")
    return s[:n] + "…" if len(s) > n else s


def _render_tasks_html(tasks: list) -> str:
    """Render pending tasks as styled HTML tabs."""
    tabs = []
    for i, t in enumerate(tasks):
        active = " ha-tab-active" if i == 0 else ""
        ctx = t.context or "{}"
        try:
            ctx_obj = json.loads(ctx)
            ctx_text = json.dumps(ctx_obj, ensure_ascii=False, indent=2) if ctx_obj else "（无）"
        except (json.JSONDecodeError, TypeError):
            ctx_text = "（无）"
        tabs.append(f"""
        <div class="ha-tab{active}" data-task="{_escape(t.task_id)}">
          <div class="ha-tab-title">任务 {i + 1} · {_escape(_truncate(t.goal or "", 30))}</div>
          <div class="ha-tab-body">
            <div class="ha-meta">到达：{_escape(t.arrived_at or "")} · task_id: <code>{_escape(t.task_id)}</code></div>
            <div class="ha-goal"><strong>目标：</strong>{_escape(t.goal or "")}</div>
            <details class="ha-ctx"><summary>上下文</summary><pre>{_escape(ctx_text)}</pre></details>
          </div>
        </div>""")
    return f'<div class="ha-tabs">{"".join(tabs)}</div>'


def _refresh_tasks(server_id: str) -> tuple:
    """Query local DB for pending tasks. Returns (html, task_ids_json, count)."""
    if not server_id or not server_id.strip():
        return ("<div class='ha-empty'>请输入身份（server_id）后开始轮询。</div>", "[]", 0)
    repo = HumanTaskRepository()
    tasks = repo.find_pending_by_server_id(server_id.strip())
    if not tasks:
        return (
            f"<div class='ha-empty'>暂无待处理任务。身份：<code>{_escape(server_id)}</code> · 已连接</div>",
            "[]",
            0,
        )
    html = _render_tasks_html(tasks) + _TAB_JS
    ids = [t.task_id for t in tasks]
    return html, json.dumps(ids), len(tasks)


def _submit_result(server_id: str, task_id: str, result_text: str) -> tuple:
    """Send task_result via HumanAgentClient WS, mark submitted in local DB."""
    if not task_id or not result_text.strip():
        return (*_refresh_tasks(server_id), "", gr.update(visible=False))
    client = get_human_agent_client()
    ok = client.send_result(task_id, success=True, output=result_text.strip())
    if not ok:
        return (
            f"<div class='ha-empty'>提交失败：HumanAgentClient 未连接到 WS 服务器。请重试。</div>",
            "[]",
            0,
            "",
            gr.update(visible=False),
        )
    # Refresh task list, clear result input, hide submit area until next timer tick
    html, ids_json, count = _refresh_tasks(server_id)
    return html, ids_json, count, "", gr.update(visible=count > 0)


def create_page(global_state_component):
    """Create the human-tasks page — identity + timer-driven task display + submit."""
    with gr.Blocks() as page:
        gr.HTML(f"<style>{_HUMAN_CSS}</style>")
        gr.HTML(_TAB_JS)
        gr.Markdown("# 我的任务（Human-in-the-Loop）")
        gr.Markdown(
            "*本页面以 Human Agent 身份接收需人工提交的任务。"
            "在下方输入身份（server_id），不同身份互不串扰；多个任务以 Tab 展示，"
            "可切换并独立提交。点击展开任务后即可填写结果并提交。每 5 秒自动扫描新任务。*"
        )

        # Hidden state: cached task IDs to avoid unnecessary HTML rewrites
        task_ids_state = gr.State(value="[]")

        # Hidden field: stores the currently active task_id (set by JS on tab click)
        active_task_id = gr.Textbox(value="", elem_id="ha-active-task", visible=False)

        with gr.Row():
            identity = gr.Textbox(
                label="Human Agent 身份（server_id）",
                value=os.getenv("HUMAN_AGENT_DEFAULT_ID", "human-test"),
                elem_id="ha-identity",
                interactive=True,
            )
            status_indicator = gr.Textbox(
                label="状态",
                value="就绪",
                interactive=False,
                scale=0,
                min_width=100,
            )

        # Timer triggers refresh
        timer = gr.Timer(5, active=True)

        # Task display area
        tasks_html = gr.HTML(
            value="<div class='ha-empty'>正在初始化…</div>",
            elem_id="ha-tabs",
        )

        # Submit area — only visible when tasks exist
        with gr.Row(visible=False) as submit_row:
            result_input = gr.Textbox(
                label="任务结果",
                placeholder="在此填写任务结果后点击「提交结果」（提交当前激活 Tab 的任务）…",
                lines=6,
                interactive=True,
            )
            submit_btn = gr.Button("提交结果", variant="primary")

        # Timer tick: refresh task list
        def on_timer(server_id):
            html, ids_json, count = _refresh_tasks(server_id)
            status = f"已连接 · {count} 个待处理任务" if count else "已连接 · 无待处理任务"
            if not server_id or not server_id.strip():
                status = "请输入身份"
            return html, ids_json, status, gr.update(visible=count > 0)

        timer.tick(on_timer, inputs=[identity], outputs=[tasks_html, task_ids_state, status_indicator, submit_row])

        # Also refresh on identity change
        def on_identity_change(server_id):
            html, ids_json, count = _refresh_tasks(server_id)
            status = f"已连接 · {count} 个待处理任务" if count else "已连接 · 无待处理任务"
            if not server_id or not server_id.strip():
                status = "请输入身份"
            return html, ids_json, status, gr.update(visible=count > 0)

        identity.change(on_identity_change, inputs=[identity], outputs=[tasks_html, task_ids_state, status_indicator, submit_row])

        # Initial load
        page.load(on_identity_change, inputs=[identity], outputs=[tasks_html, task_ids_state, status_indicator, submit_row])

        # Submit flow — uses the hidden active_task_id (set by JS tab clicks)
        def on_submit(server_id, task_ids_json, active_tid, result):
            # Determine which task to submit: prefer active_tid from JS, fallback to first pending
            if not task_ids_json:
                ids = []
            else:
                try:
                    ids = json.loads(task_ids_json)
                except (json.JSONDecodeError, TypeError):
                    ids = []
            if not ids:
                return "<div class='ha-empty'>暂无任务</div>", task_ids_json, "", gr.update(visible=False)

            task_id = active_tid.strip() if active_tid and active_tid.strip() in ids else ids[0]
            html, new_ids, _, clear_input, submit_vis = _submit_result(server_id, task_id, result)
            return html, new_ids, clear_input, submit_vis

        submit_btn.click(
            on_submit,
            inputs=[identity, task_ids_state, active_task_id, result_input],
            outputs=[tasks_html, task_ids_state, result_input, submit_row],
        )

    return page