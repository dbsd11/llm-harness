# Offline HTML export for scenario detail — 自包含离线 HTML 导出
# Renders scenario info + conversation history by business meaning (not raw
# JSON). Self-contained: inline CSS only, no external resources.
import html
import json
import os
import re
import tempfile
from datetime import datetime
from typing import Any, Dict, List, Optional

from api_client import api_client


# ── scenario type / event type → 中文 ──────────────────────────────────────

_SCENARIO_TYPE_ZH = {
    "simple_qa": "问答",
    "code_execution": "代码执行",
    "debate": "辩论",
}

# Full event-type → 中文 mapping. Fallback: segment-by-segment translation.
_EVENT_TYPE_ZH = {
    "scenario.created": "场景创建",
    "scenario.started": "场景启动",
    "scenario.completed": "场景完成",
    "scenario.failed": "场景失败",
    "scenario.stopped": "场景停止",
    "scenario.awaiting_review": "等待人工验收",
    "scenario.review_cycle_advanced": "验收周期推进",
    "scenario.qa_started": "问答开始",
    "scenario.qa_completed": "问答完成",
    "scenario.qa_failed": "问答失败",
    "scenario.code_execution_started": "代码执行开始",
    "scenario.code_execution_completed": "代码执行完成",
    "scenario.code_execution_failed": "代码执行失败",
    "scenario.debate_started": "辩论开始",
    "scenario.debate_completed": "辩论完成",
    "scenario.debate_failed": "辩论失败",
    "scenario.agents_released": "Agent资源释放",
    "task.submitted": "任务提交",
    "task.started": "任务开始",
    "task.created": "子任务创建",
    "task.scheduled": "任务调度",
    "task.completed": "任务完成",
    "task.failed": "任务失败",
    "task.reviewed": "人工验收",
    "task.execution_started": "执行开始",
    "task.execution_completed": "执行完成",
    "task.execution_failed": "执行失败",
    "task.scheduling_failed": "调度失败",
    "agent.registered": "Agent注册",
    "watchdog.timeout_detected": "超时检测",
    "watchdog.recovery_attempted": "恢复尝试",
}

_EVENT_SEG_ZH = {
    "scenario": "场景", "task": "任务", "agent": "Agent", "watchdog": "看门狗",
    "qa": "问答", "code_execution": "代码执行", "debate": "辩论",
    "created": "创建", "started": "启动", "submitted": "提交", "completed": "完成",
    "failed": "失败", "stopped": "停止", "cancelled": "取消", "scheduled": "调度",
    "registered": "注册", "released": "释放", "agents_released": "资源释放",
    "reviewed": "人工验收", "awaiting_review": "等待验收", "review_cycle_advanced": "验收周期推进",
    "execution_started": "执行开始", "execution_completed": "执行完成",
    "execution_failed": "执行失败", "scheduling_failed": "调度失败",
    "timeout_detected": "超时检测", "recovery_attempted": "恢复尝试",
}


def _event_type_zh(event_type: str) -> str:
    if not event_type:
        return "事件"
    if event_type in _EVENT_TYPE_ZH:
        return _EVENT_TYPE_ZH[event_type]
    # Fallback: translate each dot-segment; keep unknown segments as-is.
    parts = [_EVENT_SEG_ZH.get(seg, seg) for seg in event_type.split(".")]
    return "·".join(parts)


def _parse_json(raw: Any) -> Optional[Dict[str, Any]]:
    """Best-effort JSON parse of a stored content/data field."""
    if isinstance(raw, dict):
        return raw
    if not raw or not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


# ── message history (shared by live Dataframe + HTML export) ──────────────

def build_message_history(scenario_id: str, trace_id: str = "", scenario: Any = None) -> List[Dict[str, Any]]:
    """
    Unified message timeline — fetched from ws_server API (single source of truth).
    """
    resp = api_client.get_scenario_messages(scenario_id)
    if resp.get("success"):
        return resp.get("messages", [])
    return []


# Event data keys that are opaque identifiers — never shown to humans.
_ID_KEYS = {
    "scenario_id", "task_id", "parent_task_id", "topic_id", "agent_run_id",
    "agent_id", "trace_id", "idempotency_key", "id", "subtask_ids",
    "subtask_count", "task_state",
}

# Event data key → Chinese label.
_DATA_KEY_ZH = {
    "name": "名称", "scenario_type": "类型", "state": "状态", "error": "错误",
    "goal": "目标", "result": "结果", "success": "是否成功", "role": "角色",
    "question": "问题", "message": "消息", "released_runs": "释放Agent数",
    "cancelled_tasks": "取消任务数", "agent_type": "Agent类型",
    "task_state": "任务状态", "question_length": "问题长度", "response_length": "回复长度",
}


# Known enum values → Chinese (scenario_type, state, etc.)
_VALUE_ZH = {
    "simple_qa": "问答", "code_execution": "代码执行", "debate": "辩论",
    "completed": "已完成", "failed": "已失败", "cancelled": "已取消",
    "running": "运行中", "initializing": "初始化中", "pending": "待处理",
    "success": "成功", "timeout": "超时",
    "scheduling": "调度", "execution": "执行",
}


def _value_text(v: Any) -> str:
    """Flatten a data value to a short human string (no JSON)."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "成功" if v else "失败"
    if isinstance(v, (dict, list)):
        # Nested structure: pull out the most meaningful inner field only
        if isinstance(v, dict):
            for k in ("output", "goal", "error", "message", "name"):
                if k in v and v[k]:
                    return _value_text(v[k])
            return ""
        return ", ".join(_value_text(x) for x in v if x)
    s = str(v)
    return _VALUE_ZH.get(s, s)


def _event_data_summary(data: Optional[Dict[str, Any]]) -> str:
    """One-line Chinese summary of an event's data payload (for the live table)."""
    if not isinstance(data, dict) or not data:
        return ""
    parts = []
    for k, v in data.items():
        if k in _ID_KEYS or v is None:
            continue
        txt = _value_text(v)
        if not txt:
            continue
        label = _DATA_KEY_ZH.get(k, k)
        parts.append(f"{label}：{txt[:100]}")
        if len(parts) >= 3:
            break
    return " | ".join(parts)


def _render_event_data_html(data: Optional[Dict[str, Any]]) -> str:
    """Semantic HTML for an event's data — Chinese labels, no ids, no JSON."""
    if not isinstance(data, dict) or not data:
        return ""
    lines = []
    for k, v in data.items():
        if k in _ID_KEYS or v is None:
            continue
        txt = _value_text(v)
        if not txt:
            continue
        label = _DATA_KEY_ZH.get(k, k)
        lines.append(
            f'<div class="ev-note"><b>{_esc(label)}</b>：{_esc(txt[:300])}</div>'
        )
    return "".join(lines)


# ── tiny inline markdown renderer (LLM reply output) ───────────────────────

def _markdown_to_html(text: str) -> str:
    """
    Minimal, safe markdown → HTML for LLM output.

    Escape first, then transform: headings, bold, horizontal rule,
    unordered/ordered lists, paragraphs (newlines). No raw HTML passes
    through. Good enough for readable transcripts; not a full MD spec.
    """
    if not text:
        return ""
    s = html.escape(text)
    lines = s.split("\n")
    out: List[str] = []
    list_open: Optional[str] = None  # 'ul' | 'ol' | None

    def close_list():
        nonlocal list_open
        if list_open:
            out.append(f"</{list_open}>")
            list_open = None

    for line in lines:
        stripped = line.strip()
        # Horizontal rule
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", stripped):
            close_list()
            out.append("<hr>")
            continue
        # Headings
        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            close_list()
            level = min(len(m.group(1)) + 2, 6)  # #→h3, ##→h4, ###→h5
            out.append(f"<h{level}>{m.group(2)}</h{level}>")
            continue
        # Unordered list
        m = re.match(r"^[-*+]\s+(.*)$", stripped)
        if m:
            if list_open != "ul":
                close_list()
                out.append("<ul>")
                list_open = "ul"
            out.append(f"<li>{m.group(1)}</li>")
            continue
        # Ordered list
        m = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if m:
            if list_open != "ol":
                close_list()
                out.append("<ol>")
                list_open = "ol"
            out.append(f"<li>{m.group(1)}</li>")
            continue
        # Blank line → paragraph break
        if not stripped:
            close_list()
            out.append("")
            continue
        # Regular text line
        close_list()
        out.append(f"<p>{stripped}</p>")

    close_list()
    # Inline bold (after block structure)
    html_body = "\n".join(out)
    html_body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html_body)
    return html_body


# ── HTML rendering ─────────────────────────────────────────────────────────

_STATE_COLORS = {
    "completed": "#16a34a", "failed": "#dc2626", "cancelled": "#6b7280",
    "running": "#2563eb", "initializing": "#2563eb",
}

_CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
       margin: 0; padding: 0; background: #f8fafc; color: #1e293b; line-height: 1.7; }
.container { max-width: 980px; margin: 0 auto; padding: 32px 24px; }
header { background: #fff; border-radius: 12px; padding: 24px 28px; margin-bottom: 24px;
         box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
h1 { margin: 0 0 10px 0; font-size: 24px; }
.meta { color: #64748b; font-size: 14px; }
.meta span { margin-right: 16px; }
.badge { display: inline-block; padding: 2px 10px; border-radius: 999px;
         color: #fff; font-size: 12px; font-weight: 600; }
section { background: #fff; border-radius: 12px; padding: 24px 28px; margin-bottom: 24px;
          box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
h2 { margin: 0 0 20px 0; font-size: 18px; border-left: 4px solid #2563eb; padding-left: 12px; }
h3 { margin: 18px 0 8px 0; font-size: 15px; color: #334155; }
.field { margin-bottom: 10px; }
.field-label { display: inline-block; min-width: 90px; color: #64748b;
               font-size: 13px; font-weight: 600; }
.field-value { color: #1e293b; }
.role-card { background: #f1f5f9; border-radius: 8px; padding: 12px 14px; margin-bottom: 8px; }
.role-card .role-name { font-weight: 600; color: #2563eb; }
.role-card .role-desc { color: #475569; font-size: 14px; margin-top: 2px; }

/* ── timeline ── */
.timeline { position: relative; padding-left: 28px; }
.timeline::before { content: ""; position: absolute; left: 7px; top: 4px; bottom: 4px;
                    width: 2px; background: #e2e8f0; }
.tl-item { position: relative; margin-bottom: 22px; }
.tl-dot { position: absolute; left: -27px; top: 6px; width: 16px; height: 16px;
          border-radius: 50%; border: 3px solid #fff; z-index: 1; }
.tl-item.dispatch .tl-dot { background: #2563eb; box-shadow: 0 0 0 2px #bfdbfe; }
.tl-item.reply .tl-dot { background: #16a34a; box-shadow: 0 0 0 2px #bbf7d0; }
.tl-item.event .tl-dot { background: #94a3b8; box-shadow: 0 0 0 2px #e2e8f0; }
.tl-item.review .tl-dot { background: #d946ef; box-shadow: 0 0 0 2px #f5d0fe; }
.tl-card { background: #fff; border-radius: 10px; padding: 14px 18px;
           box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
.tl-item.dispatch .tl-card { background: #eff6ff; }
.tl-item.reply .tl-card { background: #f0fdf4; }
.tl-item.event .tl-card { background: #f8fafc; }
.tl-item.review .tl-card { background: #fdf4ff; }
.tl-head { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 6px; }
.tl-type { font-weight: 600; font-size: 14px; color: #1e293b; }
.tl-time { font-size: 12px; color: #94a3b8; }
.tl-flow { font-size: 13px; color: #475569; margin-bottom: 8px; }
.tl-flow .arrow { color: #94a3b8; margin: 0 5px; }
.tl-body { font-size: 14px; }
.tl-body .blk { margin-bottom: 8px; }
.tl-body .blk-label { font-size: 12px; font-weight: 600; color: #64748b; }
.tl-body .blk-text { margin-top: 2px; }
.tl-body .output { line-height: 1.8; }
.tl-body .output h3, .tl-body .output h4, .tl-body .output h5 { margin: 14px 0 6px; }
.tl-body .output p { margin: 6px 0; }
.tl-body .output ul, .tl-body .output ol { margin: 6px 0; padding-left: 24px; }
.tl-body .output hr { border: none; border-top: 1px solid #e2e8f0; margin: 12px 0; }
.tl-body .output strong { color: #1e293b; }
.tl-body .ev-note { color: #475569; }
.tl-body .ev-note b { color: #1e293b; }
.tl-body .review-content { margin-top: 8px; }
.tl-body .review-decision { display: inline-block; padding: 2px 8px; border-radius: 10px;
                             color: #fff; font-size: 12px; font-weight: 600; margin-right: 8px; }
.tl-body .review-feedback { background: #fff; border-radius: 6px; padding: 8px 12px; margin-top: 6px;
                            color: #1e293b; font-size: 14px; line-height: 1.6; }
.empty { color: #94a3b8; font-style: italic; padding: 24px; text-align: center; }
footer { text-align: center; color: #94a3b8; font-size: 12px; padding: 16px 0; }
"""


def _esc(s: Any) -> str:
    return html.escape(str(s) if s is not None else "")


def _extract_trace_id(scenario) -> str:
    if scenario and scenario.context:
        try:
            return json.loads(scenario.context).get("trace_id", "") or ""
        except (json.JSONDecodeError, TypeError):
            pass
    return ""


def _render_roles(config: Dict[str, Any]) -> str:
    roles = config.get("agent_roles", {}) or {}
    parts = []
    sched = roles.get("scheduling_agent", {}).get("role")
    if sched:
        parts.append(
            '<div class="role-card"><div class="role-name">调度 Agent</div>'
            f'<div class="role-desc">{_esc(sched)}</div></div>'
        )
    for a in roles.get("execution_agents", []) or []:
        parts.append(
            '<div class="role-card">'
            f'<div class="role-name">{_esc(a.get("name", "执行 Agent"))}</div>'
            f'<div class="role-desc">{_esc(a.get("role", ""))}</div></div>'
        )
    return f'<div class="roles">{"".join(parts)}</div>' if parts else ""


def _render_config_fields(scenario_type: str, config: Dict[str, Any]) -> str:
    """Render type-specific config as labeled fields, never raw JSON."""
    type_zh = _SCENARIO_TYPE_ZH.get(scenario_type, scenario_type)
    fields = [f'<div class="field"><span class="field-label">场景类型</span>'
              f'<span class="field-value">{_esc(type_zh)}</span></div>']

    def field(label, value):
        if value is None or value == "":
            return ""
        return (f'<div class="field"><span class="field-label">{_esc(label)}</span>'
                f'<span class="field-value">{_esc(value)}</span></div>')

    if scenario_type == "simple_qa":
        fields.append(field("问题", config.get("question")))
        fields.append(field("超时时间", f"{config.get('timeout', '')} 秒" if config.get("timeout") else None))
    elif scenario_type == "code_execution":
        fields.append(field("Python 脚本", config.get("script")))
        fields.append(field("代码片段", config.get("code")))
        fields.append(field("超时时间", f"{config.get('timeout', '')} 秒" if config.get("timeout") else None))
    elif scenario_type == "debate":
        fields.append(field("辩题", config.get("topic")))
        fields.append(field("轮次", config.get("rounds")))
        fields.append(field("超时时间", f"{config.get('timeout', '')} 秒" if config.get("timeout") else None))
    else:
        # Generic: show remaining keys (excluding agent_roles) as key-value
        for k, v in config.items():
            if k == "agent_roles":
                continue
            fields.append(field(k, v))

    return "".join(f for f in fields if f)


def _render_timeline_item(entry: Dict[str, Any]) -> str:
    """Render one history entry as a timeline node (dot + card)."""
    kind = entry.get("kind", "event")
    time = _esc(entry["time"])
    type_label = _esc(entry["type"])
    sender = _esc(entry["sender"])
    receiver = _esc(entry["receiver"])
    detail = entry.get("detail") or {}

    head = (
        f'<div class="tl-head">'
        f'<span class="tl-type">{type_label}</span>'
        f'<span class="tl-time">{time}</span>'
        f'</div>'
    )
    flow = (f'<div class="tl-flow">{sender}'
            f'<span class="arrow">→</span>{receiver}</div>')

    if kind == "dispatch":
        parts = []
        if detail.get("goal"):
            parts.append(f'<div class="blk"><div class="blk-label">目标</div>'
                         f'<div class="blk-text">{_esc(detail["goal"])}</div></div>')
        if detail.get("role"):
            parts.append(f'<div class="blk"><div class="blk-label">指派角色</div>'
                         f'<div class="blk-text">{_esc(detail["role"])}</div></div>')
        if detail.get("question"):
            parts.append(f'<div class="blk"><div class="blk-label">问题</div>'
                         f'<div class="blk-text">{_esc(detail["question"])}</div></div>')
        body = f'<div class="tl-body">{"".join(parts)}</div>'
    elif kind == "reply":
        output = detail.get("output", "")
        status = detail.get("success")
        status_html = '<div class="blk" style="color:#dc2626">⚠ 执行失败</div>' if status is False else ""
        body = (f'<div class="tl-body">{status_html}'
                f'<div class="output">{_markdown_to_html(output)}</div></div>')
    elif kind == "review":
        decision = _esc(detail.get("decision", ""))
        decision_color = detail.get("decision_color", "#d946ef")
        feedback = _esc(detail.get("feedback", ""))
        goal = _esc(detail.get("goal", ""))
        agent_role = _esc(detail.get("agent_role", ""))

        decision_html = f'<span class="review-decision" style="background:{decision_color}">{decision}</span>'
        goal_html = f'<span style="color:#475569">{goal}</span>' if goal else ""
        role_html = f'<div style="font-size:12px;color:#64748b;margin-top:4px">执行角色：{agent_role}</div>' if agent_role else ""

        feedback_html = ""
        if feedback:
            feedback_html = f'<div class="review-feedback"><div style="font-size:12px;font-weight:600;color:#64748b;margin-bottom:4px">审核反馈</div>{feedback}</div>'

        body = (f'<div class="tl-body"><div class="review-content">'
                f'{decision_html}{goal_html}{role_html}{feedback_html}'
                f'</div></div>')
    else:  # event
        ev_html = _render_event_data_html(detail.get("data"))
        body = f'<div class="tl-body">{ev_html}</div>'

    return (
        f'<div class="tl-item {kind}">'
        f'<div class="tl-dot"></div>'
        f'<div class="tl-card">{head}{flow}{body}</div>'
        f'</div>'
    )


def generate_scenario_export_html(scenario_id: str) -> str:
    """Render a fully self-contained HTML document for a scenario via ws_server API."""
    # Fetch scenario from ws_server API
    resp = api_client.get_scenario(scenario_id)
    if not resp.get("success"):
        return ("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
                "<title>场景未找到</title></head><body><div class='container'>"
                "<h1>场景未找到</h1><p class='empty'>scenario_id: " + _esc(scenario_id) + "</p>"
                "</div></body></html>")

    data = resp["scenario"]
    config = data.get("config", {}) or {}

    # Build a scenario-like object for rendering
    class _S:
        pass
    scenario = _S()
    scenario.scenario_id = data.get("scenario_id", scenario_id)
    scenario.name = data.get("name", "")
    scenario.state = data.get("state", "unknown")
    scenario.scenario_type = data.get("scenario_type", "unknown")
    scenario.created_at = data.get("created_at")
    scenario.started_at = data.get("started_at")
    scenario.completed_at = data.get("completed_at")
    scenario.config = json.dumps(config)
    scenario.context = json.dumps(data.get("context", {}))
    scenario.description = data.get("description", "")

    trace_id = _extract_trace_id(scenario)
    scenario_type = data.get("scenario_type", "unknown")

    # Chronological (asc) for a readable top-to-bottom transcript
    history = sorted(build_message_history(scenario_id, trace_id, scenario), key=lambda r: r["time"])

    state = data.get("state", "unknown")
    badge_color = _STATE_COLORS.get(state, "#6b7280")
    name = scenario.name or scenario_id

    header = f"""
<header>
  <h1>{_esc(name)}</h1>
  <div class="meta">
    <span class="badge" style="background:{badge_color}">{_esc(state)}</span>
    <span>场景ID：{_esc(scenario_id)}</span>
  </div>
  <div class="meta" style="margin-top:8px">
    <span>创建：{_esc(scenario.created_at)}</span>
    <span>开始：{_esc(scenario.started_at)}</span>
    <span>完成：{_esc(scenario.completed_at)}</span>
  </div>
</header>"""

    info = f"""
<section>
  <h2>场景信息</h2>
  {_render_roles(config)}
  <h3>场景配置</h3>
  {_render_config_fields(scenario_type, config)}
</section>"""

    if not history:
        history_html = '<p class="empty">暂无场景日志</p>'
    else:
        history_html = '<div class="timeline">' + \
                        "".join(_render_timeline_item(e) for e in history) + \
                        '</div>'

    history_section = f"""
<section>
  <h2>场景日志</h2>
  {history_html}
</section>"""

    exported_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    footer = f'<footer>导出于 {_esc(exported_at)} · agent-server-platform</footer>'

    return (
        "<!DOCTYPE html>"
        '<html lang="zh-CN">'
        '<head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{_esc(name)} — 场景导出</title>'
        f'<style>{_CSS}</style></head>'
        '<body><div class="container">'
        f'{header}{info}{history_section}{footer}'
        '</div></body></html>'
    )


def _sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_")
    return cleaned[:40] or "scenario"


def write_export_file(scenario_id: str) -> Optional[str]:
    """Write the export HTML to a temp file and return its path for DownloadButton."""
    if not scenario_id:
        return None
    html_content = generate_scenario_export_html(scenario_id)
    resp = api_client.get_scenario(scenario_id)
    name = resp.get("scenario", {}).get("name", "scenario") if resp.get("success") else "scenario"
    base = _sanitize_filename(name)
    filename = f"{base}_{scenario_id[:8]}.html"
    tmp = tempfile.NamedTemporaryFile(
        prefix="scenario_export_", suffix=f"_{filename}", delete=False, mode="w", encoding="utf-8"
    )
    tmp.write(html_content)
    tmp.close()
    return tmp.name


# ── self-check ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # ponytail: one runnable check — markdown renderer must escape markup
    out = _markdown_to_html("### 标题\n\n**粗体** & <script>x</script>\n- a\n- b")
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "<h5>标题</h5>" in out and "<strong>粗体</strong>" in out
    assert "<ul>" in out and "<li>a</li>" in out
    print("markdown self-check OK")
