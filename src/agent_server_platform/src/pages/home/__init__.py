# Home page - Dashboard + 场景管理 Chatbot 浮窗
#
# Thin presentation layer: dashboard stats and chat proxied to ws_server API.
# Local DB only used for assistant_message (chat history persistence).
from datetime import datetime

import gradio as gr

from api_client import api_client
from database.repositories.assistant_message_repository import AssistantMessageRepository
from pages.home.compression import compress_if_needed
from core.local_llm_client import llm_client

# Pre-fetch assistant message rendering
ASSISTANT_EMPTY_PREVIEW = "暂无草稿。通过对话描述想创建的场景，草稿会在此预览，确认无误后再保存。"


# ── Floating UI styles ──────────────────────────────────────────────
_DRAWER_CSS = """
.fab {
  position: fixed !important;
  left: 24px; bottom: 24px;
  z-index: 9999;
  width: 56px !important;
  height: 56px !important;
  border-radius: 50% !important;
  padding: 0 !important;
  font-size: 24px !important;
  box-shadow: 0 4px 14px rgba(0,0,0,0.25) !important;
}
.modal-backdrop {
  position: fixed !important;
  inset: 0 !important;
  z-index: 9998;
  background: rgba(15, 23, 42, 0.45);
  display: flex !important;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.modal-card {
  width: 560px;
  max-width: 96vw;
  max-height: 88vh;
  overflow-y: auto;
  background: #ffffff;
  border-radius: 14px;
  box-shadow: 0 12px 36px rgba(0,0,0,0.3);
  padding: 18px 20px !important;
}
#msg-context-menu {
    position: fixed;
    background: white;
    border: 1px solid #ccc;
    border-radius: 4px;
    padding: 4px 0;
    box-shadow: 0 2px 8px rgba(0,0,0,0.15);
    z-index: 9999;
}
#msg-context-menu .menu-item {
    padding: 6px 16px;
    cursor: pointer;
    font-size: 14px;
}
#msg-context-menu .menu-item:hover {
    background: #f0f0f0;
}
.message-wrapper {
    position: relative;
}
.message-wrapper .delete-btn {
    position: absolute;
    top: 5px;
    right: 5px;
    background: rgba(239, 68, 68, 0.9);
    color: white;
    border: none;
    border-radius: 50%;
    width: 24px;
    height: 24px;
    font-size: 14px;
    cursor: pointer;
    opacity: 0;
    transition: opacity 0.2s;
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 10;
}
.message-wrapper:hover .delete-btn {
    opacity: 1;
}
.message-wrapper .delete-btn:hover {
    background: rgba(220, 38, 38, 1);
}
"""


def create_page(global_state_component):
    """Create home page — Dashboard + 场景管理 Chatbot via ws_server API."""
    with gr.Blocks() as page:
        gr.HTML(f"<style>{_DRAWER_CSS}</style>")

        gr.Markdown("# Agent Server Platform - Dashboard")
        gr.Markdown("*Welcome to the universal agent-server platform*")

        # ── Dashboard stats (via ws_server API) ─────────────────────
        with gr.Row():
            with gr.Column():
                agent_count = gr.Number(label="Registered Agents", value=0, interactive=False)
            with gr.Column():
                task_count = gr.Number(label="Total Tasks", value=0, interactive=False)
            with gr.Column():
                running_tasks = gr.Number(label="Running Tasks", value=0, interactive=False)
            with gr.Column():
                scenario_count = gr.Number(label="Scenarios", value=0, interactive=False)

        gr.Markdown("## Recent Events")
        event_log = gr.Dataframe(
            headers=["Timestamp", "Event Type", "Data"],
            label="Recent Events",
            wrap=True,
        )

        with gr.Accordion("System Information", open=False):
            gr.Markdown(f"""
            **Platform Version**: 2.0.0 (presentation-only layer)
            **Backend**: ws_server @ {api_client.base_url}
            **Database**: SQLite (events table only)
            **Started**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

            **Architecture**:
            - Presentation Layer: Gradio UI + events sync
            - Core Backend: ws_server (aiohttp) — owns all business logic
            """)

        # ── Floating chatbot modal ──────────────────────────────────
        modal_open = gr.State(value=False)
        pending_state = gr.State(value=None)
        saved_id_state = gr.State(value=None)
        dirty_state = gr.State(value=False)
        pending_wf_state = gr.State(value=None)
        saved_wf_id_state = gr.State(value=None)
        wf_dirty_state = gr.State(value=False)

        fab_btn = gr.Button("💬", elem_classes="fab")

        with gr.Column(visible=False, elem_classes="modal-backdrop") as modal_backdrop:
            with gr.Column(elem_classes="modal-card"):
                with gr.Row():
                    gr.Markdown("#### 场景管理助手")
                    close_btn = gr.Button("✕", size="sm", scale=0)
                gr.Markdown("*对话管理场景：查看/总结进行中的场景，或多轮对话创建新场景（确认后才落库）。*")
                chatbot = gr.Chatbot(
                    label="助手对话",
                    height=420,
                    placeholder="对我说：查看进行中的场景 / 帮我创建一个辩论场景…",
                    type="messages",
                )
                delete_index_box = gr.Textbox(visible=False, elem_id="delete-index-box")
                delete_btn = gr.Button(visible=False, elem_id="delete-btn")
                with gr.Row():
                    user_input = gr.Textbox(
                        placeholder="例如：列出所有场景；总结场景 <id>；帮我创建一个问答场景…",
                        scale=4, show_label=False, autofocus=True,
                    )
                    send_btn = gr.Button("发送", variant="primary", scale=1)

                preview_md = gr.Markdown(ASSISTANT_EMPTY_PREVIEW, visible=False)
                with gr.Row(visible=False) as draft_actions:
                    confirm_btn = gr.Button("✅ 确认保存到数据库", variant="primary")
                    discard_btn = gr.Button("放弃草稿")

                wf_preview_md = gr.Markdown("暂无 Workflow 草稿。", visible=False)
                with gr.Row(visible=False) as wf_draft_actions:
                    wf_confirm_btn = gr.Button("✅ 确认保存 Workflow", variant="primary")
                    wf_discard_btn = gr.Button("放弃 Workflow 草稿")

        # ── Dashboard data loading (via ws_server API) ────────────────

        def load_stats():
            """Load dashboard counts from ws_server API."""
            scenarios_resp = api_client.list_scenarios()
            tasks_resp = api_client.list_tasks()
            servers_resp = api_client.list_servers()

            n_scenarios = len(scenarios_resp.get("scenarios", []))
            n_tasks = len(tasks_resp.get("tasks", []))
            n_running = len([t for t in tasks_resp.get("tasks", [])
                           if t.get("state") == "running"])
            n_agents = len(servers_resp.get("servers", []))
            return n_agents, n_tasks, n_running, n_scenarios

        def load_recent_events():
            """Load recent events from ws_server API."""
            resp = api_client.list_events(limit=10)
            events = resp.get("events", [])
            # Also check local events table (synced by WSEventSubscriber)
            from database.repositories.event_repository import EventRepository
            local = EventRepository().find_recent(limit=10)
            if local:
                result = []
                for e in local:
                    result.append([str(e.timestamp), e.event_type, e.data])
                return result
            return [[e.get("timestamp", ""), e.get("event_type", ""), e.get("data", "")]
                    for e in events]

        page.load(load_stats, outputs=[agent_count, task_count, running_tasks, scenario_count])
        page.load(load_recent_events, outputs=[event_log])

        # ── Modal open/close ────────────────────────────────────────
        def on_open():
            history = load_history()
            return True, gr.update(visible=True), history

        def on_close():
            return False, gr.update(visible=False)

        fab_btn.click(on_open, None, [modal_open, modal_backdrop, chatbot])
        close_btn.click(on_close, None, [modal_open, modal_backdrop])

        # ── Chat history persistence (local DB only for this) ───────
        msg_repo = AssistantMessageRepository()
        SESSION_ID = "assistant_default"

        def load_history():
            summary = msg_repo.find_latest_summary(SESSION_ID)
            messages = msg_repo.find_by_session(SESSION_ID, limit=100)
            history_entries = []
            if summary:
                history_entries.append({
                    "role": "assistant",
                    "content": f"📝 **之前的对话摘要**: {summary.content}",
                    "id": None,
                })
            for m in messages:
                history_entries.append({"role": m.role, "content": m.content, "id": m.id})
            return history_entries

        def save_message(role: str, content: str):
            return msg_repo.save(role, content, SESSION_ID)

        def delete_message_by_id(msg_id: int, current_history):
            msg_repo.delete_message(msg_id)
            return [m for m in current_history if m.get("id") != msg_id]

        # ── Draft preview helpers ────────────────────────────────────
        def _render_preview(pending):
            """Simple markdown preview of scene-spec draft."""
            if not pending or not isinstance(pending, dict):
                return ASSISTANT_EMPTY_PREVIEW
            lines = ["### 场景草稿预览", ""]
            stype = pending.get("scenario_type", "?")
            config = pending.get("config") or {}
            lines.append(f"- **类型**：{stype}")
            lines.append(f"- **名称**：{pending.get('name', '')}")
            if pending.get("description"):
                lines.append(f"- **描述**：{pending['description']}")
            roles = config.get("agent_roles") or {}
            sched = (roles.get("scheduling_agent") or {}).get("role")
            if sched:
                lines.append(f"- **调度 Agent**：{sched}")
            exec_agents = roles.get("execution_agents") or []
            for a in exec_agents:
                if isinstance(a, dict):
                    name = a.get('name', '')
                    role = a.get('role', '')
                    sid = a.get('server_id', '')
                    if sid:
                        lines.append(f"- **执行 Agent**：{name}（{role}）→ 服务器: `{sid}`")
                    else:
                        lines.append(f"- **执行 Agent**：{name}（{role}）")
            if config.get("question"):
                lines.append(f"- **问题**：{config['question']}")
            if config.get("script") or config.get("code"):
                lines.append("- **代码**：已填写")
            if config.get("timeout"):
                lines.append(f"- **超时**：{config['timeout']} 秒")
            if config.get("manual_acceptance"):
                lines.append("- **人工验收**：已启用")
            return "\n".join(lines)

        def _draft_updates(pending, dirty):
            show_actions = bool(pending) and dirty
            return (
                gr.update(visible=bool(pending), value=_render_preview(pending)),
                gr.update(visible=show_actions),
            )

        def _wf_draft_updates(pending_wf, wf_dirty):
            show_actions = bool(pending_wf) and wf_dirty
            if pending_wf and isinstance(pending_wf, dict):
                wf_preview = _render_wf_preview(pending_wf)
            else:
                wf_preview = "暂无 Workflow 草稿。"
            return (
                gr.update(visible=bool(pending_wf), value=wf_preview),
                gr.update(visible=show_actions),
            )

        def _render_wf_preview(pending_wf):
            """Render workflow-spec draft as markdown preview."""
            if not pending_wf or not isinstance(pending_wf, dict):
                return "暂无 Workflow 草稿。"
            lines = ["### Workflow 草稿预览", ""]
            lines.append(f"- **名称**：{pending_wf.get('name', '')}")
            if pending_wf.get("description"):
                lines.append(f"- **描述**：{pending_wf['description']}")
            input_schema = pending_wf.get("input_schema") or {}
            props = input_schema.get("properties", {})
            if props:
                lines.append(f"- **输入参数**：{', '.join(props.keys())}")
            agent_roles = pending_wf.get("agent_roles") or {}
            if agent_roles:
                lines.append("- **Agent 角色**：")
                for rname, rinfo in agent_roles.items():
                    if isinstance(rinfo, dict):
                        lines.append(f"  - `{rname}`: {rinfo.get('role', '')}")
            steps = pending_wf.get("steps") or []
            if steps:
                lines.append(f"- **DAG 步骤**（{len(steps)} 步）：")
                for s in steps:
                    if isinstance(s, dict):
                        sid = s.get("step_id", "?")
                        role = s.get("agent_role", "?")
                        goal = s.get("goal_template", "")
                        deps = s.get("depends_on") or []
                        dep_str = f" ← {','.join(deps)}" if deps else ""
                        lines.append(f"  - `{sid}` [{role}]: {goal}{dep_str}")
            return "\n".join(lines)

        # ── Chat handlers ────────────────────────────────────────────

        def on_delete_from_menu(index_str):
            try:
                msg_id = int(index_str)
                if msg_repo.delete_pair(msg_id) > 0:
                    messages = msg_repo.find_by_session(SESSION_ID)
                    return [{"role": m.role, "content": m.content, "id": m.id} for m in messages]
                return []
            except (ValueError, TypeError):
                return []

        def on_send(user_text, chatbot_history, pending, dirty, saved_id,
                     pending_wf, wf_dirty, saved_wf_id):
            user_text = (user_text or "").strip()
            if not user_text:
                pv, act = _draft_updates(pending, dirty)
                wv, wact = _wf_draft_updates(pending_wf, wf_dirty)
                return (chatbot_history, pending, dirty, saved_id, pv, act, gr.update(),
                        pending_wf, wf_dirty, saved_wf_id, wv, wact)

            # Compress if needed
            if compress_if_needed(SESSION_ID, msg_repo, llm_client):
                chatbot_history = load_history()

            history = list(chatbot_history or [])
            history.append({"role": "user", "content": user_text})

            # Save user message
            user_msg_id = save_message("user", user_text)
            if user_msg_id:
                history[-1]["id"] = user_msg_id

            # Call ws_server chat API
            history_for_api = [{"role": m["role"], "content": m["content"]} for m in history]
            summary = msg_repo.find_latest_summary(SESSION_ID)
            resp = api_client.chat(
                message=user_text,
                session_id=SESSION_ID,
                history=history_for_api,
                scene_id=saved_id,
                pending_scene=pending,
                pending_workflow=pending_wf,
                workflow_id=saved_wf_id,
            )

            assistant_text = resp.get("reply", "（助手暂时没有响应，请重试。）")
            new_pending = resp.get("pending_scene", pending)
            new_saved_id = resp.get("scene_id", saved_id)

            new_pending_wf = resp.get("pending_workflow", pending_wf)
            new_saved_wf_id = resp.get("workflow_id", saved_wf_id)

            history.append({"role": "assistant", "content": assistant_text})
            assistant_msg_id = save_message("assistant", assistant_text)
            if assistant_msg_id:
                history[-1]["id"] = assistant_msg_id

            changed = new_pending is not None and new_pending != pending
            loaded_existing = bool(new_saved_id) and (new_saved_id != saved_id)
            new_dirty = dirty or changed or loaded_existing
            pv, act = _draft_updates(new_pending, new_dirty)

            wf_changed = new_pending_wf is not None and new_pending_wf != pending_wf
            wf_loaded = bool(new_saved_wf_id) and (new_saved_wf_id != saved_wf_id)
            new_wf_dirty = wf_dirty or wf_changed or wf_loaded
            wv, wact = _wf_draft_updates(new_pending_wf, new_wf_dirty)

            return (history, new_pending, new_dirty, new_saved_id, pv, act, "",
                    new_pending_wf, new_wf_dirty, new_saved_wf_id, wv, wact)

        def on_confirm(chatbot_history, pending, saved_id, dirty):
            history = list(chatbot_history or [])
            if not pending or not dirty:
                history.append({"role": "assistant",
                               "content": "⚠️ 当前没有未保存的修改。先通过对话调整场景吧。"})
                pv, act = _draft_updates(pending, dirty)
                return history, pending, saved_id, dirty, pv, act

            # Save via ws_server API
            resp = api_client.chat_save(
                scene_spec=pending,
                session_id=SESSION_ID,
                scene_id=saved_id,
            )
            if resp.get("success"):
                action = "已更新" if saved_id else "已创建"
                sid = resp.get("scenario_id", "")
                history.append({"role": "assistant",
                               "content": (f"✅ 场景{action}！`scenario_id={sid}`\n\n"
                                           "可继续对话修改，或到「Scenario Dashboard」启动。")})
                pv, act = _draft_updates(None, False)
                return history, None, sid, False, pv, act
            history.append({"role": "assistant",
                           "content": f"❌ 保存失败：{resp.get('error', 'Unknown')}\n草稿已保留。"})
            pv, act = _draft_updates(pending, dirty)
            return history, pending, saved_id, dirty, pv, act

        def on_discard(chatbot_history, pending, saved_id):
            history = list(chatbot_history or [])
            history.append({"role": "assistant",
                           "content": "已放弃当前草稿。"})
            pv, act = _draft_updates(None, False)
            return history, None, None, False, pv, act

        def on_wf_confirm(chatbot_history, pending_wf, saved_wf_id, wf_dirty):
            history = list(chatbot_history or [])
            if not pending_wf or not wf_dirty:
                history.append({"role": "assistant",
                               "content": "⚠️ 当前没有未保存的 Workflow 修改。先通过对话调整吧。"})
                wv, wact = _wf_draft_updates(pending_wf, wf_dirty)
                return history, pending_wf, saved_wf_id, wf_dirty, wv, wact

            resp = api_client.chat_workflow_save(
                workflow_spec=pending_wf,
                session_id=SESSION_ID,
                workflow_id=saved_wf_id,
            )
            if resp.get("success"):
                action = "已更新" if saved_wf_id else "已创建"
                wid = resp.get("workflow_id", "")
                history.append({"role": "assistant",
                               "content": (f"✅ Workflow {action}！`workflow_id={wid[:8]}`\n\n"
                                           "可到「Workflow DAG Management」查看或执行。")})
                wv, wact = _wf_draft_updates(None, False)
                return history, None, wid, False, wv, wact
            history.append({"role": "assistant",
                           "content": f"❌ Workflow 保存失败：{resp.get('error', 'Unknown')}\n草稿已保留。"})
            wv, wact = _wf_draft_updates(pending_wf, wf_dirty)
            return history, pending_wf, saved_wf_id, wf_dirty, wv, wact

        def on_wf_discard(chatbot_history, pending_wf, saved_wf_id):
            history = list(chatbot_history or [])
            history.append({"role": "assistant",
                           "content": "已放弃当前 Workflow 草稿。"})
            wv, wact = _wf_draft_updates(None, False)
            return history, None, None, False, wv, wact

        # ── Page load + wiring ───────────────────────────────────────
        def on_page_load():
            return load_history()

        def on_delete_message(evt: gr.SelectData, chatbot_history):
            if not chatbot_history or not evt.index:
                return chatbot_history
            msg_index = evt.index[0] if isinstance(evt.index, tuple) else evt.index
            if msg_index >= len(chatbot_history):
                return chatbot_history
            msg = chatbot_history[msg_index]
            msg_id = msg.get("id")
            if msg_id:
                return delete_message_by_id(msg_id, chatbot_history)
            return chatbot_history

        send_outputs = [chatbot, pending_state, dirty_state, saved_id_state,
                        preview_md, draft_actions, user_input,
                        pending_wf_state, wf_dirty_state, saved_wf_id_state,
                        wf_preview_md, wf_draft_actions]
        send_inputs = [user_input, chatbot, pending_state, dirty_state, saved_id_state,
                       pending_wf_state, wf_dirty_state, saved_wf_id_state]
        send_btn.click(on_send, send_inputs, send_outputs)
        confirm_btn.click(on_confirm, [chatbot, pending_state, saved_id_state, dirty_state],
                          [chatbot, pending_state, saved_id_state, dirty_state, preview_md, draft_actions])
        discard_btn.click(on_discard, [chatbot, pending_state, saved_id_state],
                          [chatbot, pending_state, saved_id_state, dirty_state, preview_md, draft_actions])
        wf_confirm_btn.click(on_wf_confirm,
                             [chatbot, pending_wf_state, saved_wf_id_state, wf_dirty_state],
                             [chatbot, pending_wf_state, saved_wf_id_state, wf_dirty_state,
                              wf_preview_md, wf_draft_actions])
        wf_discard_btn.click(on_wf_discard,
                             [chatbot, pending_wf_state, saved_wf_id_state],
                             [chatbot, pending_wf_state, saved_wf_id_state, wf_dirty_state,
                              wf_preview_md, wf_draft_actions])

        page.load(on_page_load, outputs=[chatbot])

        def on_chatbot_clear():
            msg_repo.clear_session(SESSION_ID)
            return []

        chatbot.clear(fn=on_chatbot_clear, outputs=[chatbot])
        chatbot.select(on_delete_message, [chatbot], [chatbot])

        delete_btn.click(
            fn=on_delete_from_menu,
            inputs=[delete_index_box],
            outputs=[chatbot]
        )

        # Inject context-menu JS (unchanged)
        gr.HTML("""<script>
(function(){
  let menu=null, currentDeleteBtn=null;
  function createContextMenu(x,y,msgIndex){
    if(menu)menu.remove();
    menu=document.createElement('div');
    menu.id='msg-context-menu';
    menu.style.left=x+'px';
    menu.style.top=y+'px';
    var item=document.createElement('div');
    item.className='menu-item';
    item.textContent='🗑️ 删除此消息';
    item.onclick=function(){
      var ib=document.getElementById('delete-index-box');
      var db=document.getElementById('delete-btn');
      if(ib&&db){ib.value=msgIndex;db.click();}
      menu.remove();menu=null;
    };
    menu.appendChild(item);document.body.appendChild(menu);
  }
  function attachContextMenu(){
    var cb=document.querySelector('.chatbot');
    if(!cb){setTimeout(attachContextMenu,500);return;}
    cb.addEventListener('contextmenu',function(e){
      var t=e.target;
      while(t&&t!==cb){
        if(t.classList&&(t.classList.contains('message')||t.closest('[class*="message"]'))){
          e.preventDefault();
          var msgs=cb.querySelectorAll('[class*="message"]');
          for(var i=0;i<msgs.length;i++){
            if(msgs[i]===t||msgs[i].contains(t)){
              createContextMenu(e.clientX,e.clientY,i+1);
              break;
            }
          }
          return;
        }
        t=t.parentElement;
      }
    });
    document.addEventListener('click',function(e){
      if(menu&&!menu.contains(e.target)){menu.remove();menu=null;}
      if(currentDeleteBtn&&!currentDeleteBtn.contains(e.target)){removeDeleteButtons();}
    });
  }
  function removeDeleteButtons(){
    document.querySelectorAll('.delete-btn').forEach(function(b){b.remove();});
    currentDeleteBtn=null;
  }
  attachContextMenu();
})();
</script>""")

    return page