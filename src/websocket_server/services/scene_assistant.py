# 场景管理 Chatbot 大脑 — 纯逻辑，与 Gradio UI 解耦
#
# 两种能力：
#   1. 管理：查看/总结已有场景（复用 build_message_history 聚合 messages+events）
#   2. 创建：多轮对话定义新场景，每帧可附带 ```scene-spec``` 草稿，确认后才落库
#
# 复用：llm_client.chat、decompose_goal 的 fenced-JSON 解析模式、
#       scenario_manager.create_scenario（落库前不写任何东西）。
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from core.llm_client import llm_client
from database.repositories.scenario_repository import ScenarioRepository
from core.export_html import build_message_history
from scenarios.scenario_manager import scenario_manager
from logger import logger


# ── 常量 ───────────────────────────────────────────────────────────────────

SCENARIO_TYPES = ("simple_qa", "code_execution")

_SCENARIO_TYPE_ZH = {
    "simple_qa": "问答",
    "code_execution": "代码执行",
}

# 各类型在 config 中的必填字段（与子类 initialize 校验对齐，但不调用 initialize 以免副作用）
_REQUIRED_FIELD = {
    "simple_qa": ("question", "问题"),
    "code_execution": ("script_or_code", "Python 脚本或代码片段"),
}

EMPTY_PREVIEW = "暂无草稿。通过对话描述想创建的场景，草稿会在此预览，确认无误后再保存。"

_SPEC_FENCE_RE = re.compile(r"```scene-spec\s*(\{.*?\})\s*```", re.DOTALL)
# 修改意图关键词（明确动作动词，避免误命中纯查询）
_MODIFY_KEYWORDS = ("修改", "编辑", "调整", "更新", "变更", "重命名", "改一下", "改成",
                    "modify", "edit", "update", "rename", "change")
_ID_RE = re.compile(r"(?<![a-zA-Z0-9])([0-9a-fA-F]{4,36})(?![a-zA-Z0-9])")


# ── 上下文采集 ─────────────────────────────────────────────────────────────

def gather_scene_index() -> str:
    """返回现有场景清单文本，注入 system prompt 供助手回答场景相关问题。"""
    scenarios = ScenarioRepository().find_all()
    if not scenarios:
        return "（当前数据库中没有任何场景）"
    lines = []
    for s in scenarios:
        sid = (s.scenario_id or "")[:8]
        type_zh = _SCENARIO_TYPE_ZH.get(s.scenario_type, s.scenario_type or "?")
        lines.append(f"- [{sid}] {s.name or '未命名'} | 类型:{type_zh} | 状态:{s.state or '?'}")
    return "\n".join(lines)


def gather_human_servers() -> str:
    """返回已注册的人工 Agent 服务器清单，供 LLM 在配置 execution_agents 时引用 server_id。

    ponytail: query local DB directly — this runs inside ws_server, not the platform.
    HTTP self-call to public IP times out inside the container.
    """
    from database.repositories.execution_server_repository import ExecutionServerRepository

    try:
        servers = ExecutionServerRepository().list_all()
    except Exception as e:
        logger.warning(f"Failed to query execution servers from local DB: {e}")
        return "（无法获取人工 Agent 服务器列表）"

    # 显示所有注册的人工 Agent（包括离线的），让 LLM 知道有哪些可用
    human = [s for s in servers if getattr(s, 'source', '') == 'human_agent']
    if not human:
        return "（当前没有已注册的人工 Agent 服务器）"
    lines = []
    for s in human:
        lines.append(f"- server_id: `{s.server_id}` | 名称: {s.name or ''} | 状态: {getattr(s, 'status', 'unknown')}")
    return "\n".join(lines)


def gather_execution_servers() -> str:
    """返回已注册的执行 Agent 服务器清单（含在线状态），供 LLM 准确分配 server_id。

    ponytail: query local DB directly — this runs inside ws_server, not the platform.
    """
    from database.repositories.execution_server_repository import ExecutionServerRepository

    try:
        servers = ExecutionServerRepository().list_all()
    except Exception as e:
        logger.warning(f"Failed to query execution servers from local DB: {e}")
        return "（无法获取执行服务器列表）"

    exec_servers = [s for s in servers if getattr(s, 'source', '') != 'human_agent']
    if not exec_servers:
        return "（当前没有已注册的执行 Agent 服务器，execution_agents 中不要填写 server_id）"
    lines = []
    for s in exec_servers:
        status = getattr(s, 'status', 'unknown')
        online_mark = "✅" if status == "online" else "⚠️离线"
        lines.append(f"- server_id: `{s.server_id}` | 名称: {s.name or ''} | {online_mark}")
    return "\n".join(lines)


def get_known_server_ids() -> set:
    """返回所有已注册的服务器 ID 集合（用于校验 scene-spec 中的 server_id）。"""
    from database.repositories.execution_server_repository import ExecutionServerRepository

    try:
        servers = ExecutionServerRepository().list_all()
        return {s.server_id for s in servers if getattr(s, 'server_id', '')}
    except Exception:
        return set()


def summarize_scenario(scenario_id: str) -> str:
    """把场景元信息 + 对话历史压缩为可读文本，供 LLM 总结。

    复用 export_html.build_message_history：messages(dispatch/reply) + events
    已聚合成中文业务语义时间线。历史过长则截断最近 N 条。
    """
    repo = ScenarioRepository()
    # 支持用户输入前 8 位前缀匹配
    scenario = repo.find_by_scenario_id(scenario_id)
    if not scenario and len(scenario_id) >= 4:
        for s in repo.find_all():
            if (s.scenario_id or "").startswith(scenario_id):
                scenario = s
                break
    if not scenario:
        return f"未找到场景：{scenario_id}"

    trace_id = ""
    try:
        trace_id = json.loads(scenario.context or "{}").get("trace_id", "") or ""
    except (json.JSONDecodeError, TypeError):
        pass

    history = build_message_history(scenario.scenario_id, trace_id)
    # 截断：保留最近 60 条，避免上下文爆炸
    MAX = 60
    truncated = len(history) > MAX
    shown = history[-MAX:] if truncated else history

    type_zh = _SCENARIO_TYPE_ZH.get(scenario.scenario_type, scenario.scenario_type)
    lines = [
        f"场景：{scenario.name}（{type_zh}）  状态：{scenario.state}",
        f"scenario_id：{scenario.scenario_id}",
    ]
    if shown:
        lines.append(f"对话/事件时间线（共 {len(history)} 条{f'，此处仅展示最近 {MAX} 条' if truncated else ''}）：")
        for e in shown:
            lines.append(f"  [{e.get('time', '')}] {e.get('type', '')} "
                         f"{e.get('sender', '')}→{e.get('receiver', '')}：{e.get('content', '')}")
    else:
        lines.append("（暂无对话/事件记录）")
    return "\n".join(lines)


# ── 草稿解析 / 校验 / 落库 ──────────────────────────────────────────────────

def parse_scene_spec(text: str) -> Optional[Dict[str, Any]]:
    """从 LLM 回复中抽取 ```scene-spec {...} ``` 代码块并解析为 dict。无则 None。"""
    if not text:
        return None
    m = _SPEC_FENCE_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as e:
        logger.warning(f"scene-spec JSON 解析失败: {e}")
        return None


def validate_scene_spec(spec: Dict[str, Any]) -> Tuple[bool, str]:
    """校验草稿结构。与子类 initialize 规则对齐，但不调用 initialize。"""
    if not isinstance(spec, dict):
        return False, "草稿不是有效对象。"
    stype = spec.get("scenario_type")
    if stype not in SCENARIO_TYPES:
        return False, f"scenario_type 必须是 {SCENARIO_TYPES} 之一。"
    if not (spec.get("name") or "").strip():
        return False, "场景名称(name)不能为空。"

    config = spec.get("config") or {}
    if not isinstance(config, dict):
        return False, "config 必须是对象。"

    roles = config.get("agent_roles") or {}
    exec_agents = roles.get("execution_agents") or []
    if not any(_exec_agent_ok(a) for a in exec_agents):
        return False, "至少需要一个执行 Agent（含 name 与 role）。"

    # 校验 manual_acceptance（可选，但若存在必须是 boolean）
    if "manual_acceptance" in config:
        if not isinstance(config["manual_acceptance"], bool):
            return False, "manual_acceptance 必须是 true 或 false。"

    # 类型专属必填项（与子类 initialize 规则对齐）
    field_key, field_zh = _REQUIRED_FIELD[stype]
    if stype == "code_execution":
        if not (config.get("script") or config.get("code")):
            return False, f"代码执行场景需要 {field_zh}。"
    elif not config.get(field_key):
        return False, f"{_SCENARIO_TYPE_ZH[stype]}场景需要 {field_zh}。"
    return True, ""


def check_server_id_warnings(spec: Dict[str, Any]) -> str:
    """检查 spec 中 execution_agents 的 server_id 是否匹配已知服务器，返回警告文本（空串表示无警告）。"""
    roles = (spec.get("config") or {}).get("agent_roles") or {}
    exec_agents = roles.get("execution_agents") or []
    used_ids = {a.get("server_id") for a in exec_agents if isinstance(a, dict) and a.get("server_id")}
    if not used_ids:
        return ""
    known = get_known_server_ids()
    if not known:
        return ""
    bad = used_ids - known
    if bad:
        return f"⚠️ 以下 server_id 不存在于已注册服务器中：{', '.join(sorted(bad))}。请从【可用执行 Agent 服务器】列表中选择。"
    return ""


def _exec_agent_ok(a: Any) -> bool:
    return isinstance(a, dict) and bool(a.get("name")) and bool(a.get("role"))


def render_preview(spec: Optional[Dict[str, Any]]) -> str:
    """把草稿渲染为中文 Markdown 预览。"""
    if not spec:
        return EMPTY_PREVIEW
    stype = spec.get("scenario_type", "?")
    config = spec.get("config") or {}
    roles = config.get("agent_roles") or {}
    lines = ["### 场景草稿预览", ""]
    lines.append(f"- **类型**：{_SCENARIO_TYPE_ZH.get(stype, stype)}")
    lines.append(f"- **名称**：{spec.get('name', '')}")
    if spec.get("description"):
        lines.append(f"- **描述**：{spec['description']}")
    sched = (roles.get("scheduling_agent") or {}).get("role")
    if sched:
        lines.append(f"- **调度 Agent**：{sched}")
    for a in roles.get("execution_agents") or []:
        if isinstance(a, dict):
            name = a.get('name', '')
            role = a.get('role', '')
            sid = a.get('server_id', '')
            if sid:
                lines.append(f"- **执行 Agent**：{name}（{role}）→ 服务器: `{sid}`")
            else:
                lines.append(f"- **执行 Agent**：{name}（{role}）")
    if stype == "simple_qa" and config.get("question"):
        lines.append(f"- **问题**：{config['question']}")
    elif stype == "code_execution":
        if config.get("script"):
            lines.append("- **脚本**：已填写")
        if config.get("code"):
            lines.append("- **代码**：已填写")
    if config.get("timeout"):
        lines.append(f"- **超时**：{config['timeout']} 秒")
    if config.get("manual_acceptance"):
        lines.append("- **人工验收**：已启用")
    ok, err = validate_scene_spec(spec)
    sid_warn = check_server_id_warnings(spec)
    lines.append("")
    if sid_warn:
        lines.append(f"> {sid_warn}")
    elif ok:
        lines.append("> ✅ 校验通过，可点「确认保存到数据库」")
    else:
        lines.append(f"> ⚠️ {err}")
    return "\n".join(lines)


def save_scene(spec: Dict[str, Any], scenario_id: str = None) -> Tuple[Optional[str], str]:
    """校验通过后落库。

    - scenario_id 为空：新建场景，返回新 id。
    - scenario_id 非空：更新该已有场景（name/description/config），避免重复创建。
    返回 (scenario_id, error)。
    """
    ok, err = validate_scene_spec(spec)
    if not ok:
        return None, err
    sid_warn = check_server_id_warnings(spec)
    if sid_warn:
        return None, sid_warn
    try:
        if scenario_id:
            # 仅允许修改初始化状态的场景
            existing = ScenarioRepository().find_by_scenario_id(scenario_id)
            if not existing:
                return None, f"未找到场景：{scenario_id}（可能已被删除）"
            if existing.state != "initializing":
                return None, f"仅可修改初始化状态的场景，当前状态为「{existing.state}」，无法修改。"
            updated = scenario_manager.update_scenario(
                scenario_id,
                scenario_type=spec["scenario_type"],
                name=spec["name"],
                description=spec.get("description", "") or "",
                config=spec.get("config") or {},
            )
            if not updated:
                return None, f"未找到场景：{scenario_id}（可能已被删除）"
            return scenario_id, ""
        return scenario_manager.create_scenario(
            spec["scenario_type"],
            spec["name"],
            spec.get("description", "") or "",
            spec.get("config") or {},
        ), ""
    except Exception as e:
        logger.error(f"save_scene 失败: {e}")
        return None, f"保存失败：{e}"


def load_scenario_spec(scenario_id: str) -> Optional[Dict[str, Any]]:
    """从数据库重建草稿 spec（用于「放弃修改」回退到已保存版本）。"""
    scenario = ScenarioRepository().find_by_scenario_id(scenario_id)
    if not scenario:
        return None
    try:
        config = json.loads(scenario.config) if scenario.config else {}
    except (json.JSONDecodeError, TypeError):
        config = {}
    return {
        "scenario_type": scenario.scenario_type,
        "name": scenario.name or "",
        "description": scenario.description or "",
        "config": config,
    }


# ── system prompt ──────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """你是一个「场景管理助手」，服务于 Agent Server Platform。你有两种能力：

## 1. 管理已有场景
用户可能问"有哪些场景""进行中的场景""总结场景 X 的对话历史""查看场景配置"等。
- 场景清单见下方【场景清单】。
- 若需总结某个场景的对话历史，下方【场景聚焦】会给出该场景的时间线文本；请基于它做简洁中文总结（关键派发目标、各角色回复要点、当前进展/状态）。
- 若用户给的是场景 id 前缀但【场景聚焦】为空，提示用户确认 id。
- **查看场景配置时**：使用普通 JSON 格式展示配置，**不要使用 scene-spec 代码块**。

## 2. 创建新场景（多轮对话 + 预览）
通过对话逐步澄清需求，定义一个新场景。**只支持以下两种类型**，不得生成其它类型：
- `simple_qa`（问答）：config 需 `question`
- `code_execution`（代码执行）：config 需 `script` 或 `code`

config 形状：
```
{
  "agent_roles": {
    "scheduling_agent": {"role": "调度角色描述"},
    "execution_agents": [{"name": "Agent名", "role": "角色/专长", "server_id": "可选"}]
  },
  "manual_acceptance": false,
  "question" | "script"/"code": ...,
  "timeout": 3600
}
```

**执行 Agent（execution_agents）— 必填，至少 1 个**：
- 每个执行 Agent 代表一个角色，由调度 Agent 分配子任务。
- `server_id` 可选：指定该角色绑定到哪个执行服务器。不填则使用本地后端执行。
- **⚠️ server_id 必须严格使用【可用执行 Agent 服务器】列表中列出的 server_id，逐字复制，不得自行编造、猜测或简化。** 若列表为空则不填 server_id。
- `server_id` 可以指向 **人工 Agent 服务器**（source='human_agent'），这样该角色的任务会路由给真人操作者处理。
- 示例：`{"name": "计算专家", "role": "擅长数学计算与逻辑推理"}`、`{"name": "代码执行专家", "role": "负责执行代码并返回结果"}`
- 若用户要求某角色由人工处理，将该角色的 `server_id` 设为对应的人工 Agent 服务器即可，**不需要单独的 human_agents 配置**。

**关于人工验收（manual_acceptance）**：
- 设为 `true` 时，每个任务执行完成后会暂停等待人工审核，审核通过后才能继续下一步。
- 仅当用户明确要求「人工验收」「人工审核」「需要人工确认」等时才设为 `true`。
- 默认为 `false`（不需要人工验收）。

**重要**：当你正在**创建新场景**或**修改已有场景**时，在回复末尾附一个完整的 ```scene-spec``` 代码块，包含**全量最新配置**（不要只给增量）。格式：
```scene-spec
{
  "scenario_type": "simple_qa",
  "name": "场景名称",
  "description": "可选描述",
  "config": { ... }
}
```
- 草稿在用户点「确认保存」前不会落库，你只需保持 scene-spec 为最新。
- **若用户只是查询、查看、总结场景，绝对不要使用 scene-spec 代码块，使用普通 JSON 格式即可**。

## 3. 修改已有场景
用户可要求修改一个**已存在**的场景（如「修改场景 <id>，把问题改成…」「重命名场景 <id>」）。
- 系统会在你回复前，把该场景的**全量当前配置**载入【当前草稿】。你**必须基于该全量草稿做修改**，并在回复末尾附上修改后的**完整** scene-spec（不要只给增量、不要丢字段），以保证配置不丢失。
- 仅 `initializing` 状态的场景可修改；其它状态系统会拒绝并提示，你据实转告用户即可。
- 修改与创建用的是同一套 scene-spec 格式；不要拒绝修改请求。
- 用中文回复，简洁友好。"""


def _build_messages(user_text: str, history: List[Dict[str, Any]],
                    pending_scene: Optional[Dict[str, Any]],
                    summary_content: Optional[str] = None) -> List[Dict[str, str]]:
    """组装 LLM messages：system（含场景清单/聚焦/当前草稿）+ 历史 + 当前输入。"""
    sys = _SYSTEM_PROMPT
    sys += "\n\n【场景清单】\n" + gather_scene_index()
    sys += "\n\n【可用执行 Agent 服务器】\n" + gather_execution_servers()
    sys += "\n\n【可用人工 Agent 服务器】\n" + gather_human_servers()

    # 若用户输入疑似指向某场景 id，注入聚焦上下文
    focus = _maybe_focus(user_text)
    if focus:
        sys += "\n\n【场景聚焦】\n" + focus

    if pending_scene:
        sys += "\n\n【当前草稿（已生成、待确认）】\n```json\n" \
               + json.dumps(pending_scene, ensure_ascii=False, indent=2) + "\n```"

    if summary_content:
        sys += f"\n\n【对话历史摘要】\n{summary_content}"

    messages = [{"role": "system", "content": sys}]
    # history: gr.Chatbot type="messages" 形如 [{"role","content"}, ...]
    for h in (history or [])[-10:]:
        role = h.get("role")
        content = h.get("content")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_text})
    return messages


def _maybe_focus(user_text: str) -> str:
    """用户输入中若含疑似 scenario_id（hex 片段≥4），注入该场景历史摘要。"""
    if not user_text:
        return ""
    m = _ID_RE.search(user_text)
    if not m:
        return ""
    return summarize_scenario(m.group(1))


def _resolve_scenario(scenario_id: str):
    """按完整 id 或前缀解析场景，返回 Scenario 或 None。"""
    repo = ScenarioRepository()
    s = repo.find_by_scenario_id(scenario_id)
    if s:
        return s
    if len(scenario_id) >= 4:
        for c in repo.find_all():
            if (c.scenario_id or "").startswith(scenario_id):
                return c
    return None


def _detect_edit(user_text: str, saved_id: Optional[str],
                 pending: Optional[Dict[str, Any]]
                 ) -> Tuple[Optional[str], Optional[str]]:
    """检测用户是否要修改已有场景。

    返回 (load_target_id, error)：
    - (id, None)：应载入该场景全量配置作为草稿
    - (id, "non-initializing:<state>")：目标非初始化状态，应拒绝
    - (None, None)：无修改意图，走默认流程
    """
    if not user_text or not any(k in user_text for k in _MODIFY_KEYWORDS):
        return None, None

    m = _ID_RE.search(user_text)
    target = m.group(1) if m else saved_id
    if not target:
        return None, None  # 有修改意图但未指定场景，交给 LLM 询问

    # 已在编辑同一场景且已有草稿：保留当前草稿，不重复载入（避免丢弃用户改动）
    if saved_id and pending:
        try:
            cur = _resolve_scenario(saved_id)
            if cur and (cur.scenario_id == target or cur.scenario_id.startswith(target)
                        or target.startswith(cur.scenario_id[:len(target)])):
                return None, None
        except Exception:
            pass

    scenario = _resolve_scenario(target)
    if not scenario:
        return None, None  # 找不到，交给 LLM 提示
    if scenario.state != "initializing":
        return scenario.scenario_id, f"non-initializing:{scenario.state}"
    return scenario.scenario_id, None


def reply(user_text: str, history: List[Dict[str, Any]],
          pending_scene: Optional[Dict[str, Any]],
          saved_id: Optional[str] = None,
          summary_content: Optional[str] = None
          ) -> Tuple[str, Optional[Dict[str, Any]], str, Optional[str]]:
    """一帧对话。

    返回 (assistant_text, new_pending_scene, preview_md, new_saved_id)。
    修改已有场景时：载入全量配置到草稿并绑定 saved_id，后续保存走更新。
    """
    if not llm_client.client:
        return ("⚠️ 未配置 LLM（DASHSCOPE_API_KEY 缺失），对话功能不可用。"
                "请在 .env 配置后重启服务。"), pending_scene, render_preview(pending_scene), saved_id

    new_saved_id = saved_id
    new_pending = pending_scene

    # 修改已有场景：先载入全量配置，确保 LLM 基于完整草稿修改（不丢字段）
    target, err = _detect_edit(user_text, saved_id, pending_scene)
    if err and err.startswith("non-initializing"):
        state = err.split(":", 1)[1]
        return (f"⚠️ 该场景当前状态为「{state}」，仅 `initializing` 状态的场景可修改。"
                "如需调整，请先在 Scenario Dashboard 停止/重建场景。"), \
            pending_scene, render_preview(pending_scene), saved_id

    # 检查用户是否在查看现有场景（包含场景 ID 但不是修改意图）
    # 只有当匹配到的文本确实是一个有效的场景 ID 时，才认为是查看场景
    match = _ID_RE.search(user_text)
    if match and not target:
        # 检查匹配到的文本是否真的是一个场景 ID
        potential_id = match.group(1)
        scenario_repo = ScenarioRepository()
        is_real_scenario = scenario_repo.find_by_scenario_id(potential_id) is not None
        is_viewing_scene = is_real_scenario
    else:
        is_viewing_scene = False

    if target:
        loaded = load_scenario_spec(target)
        if loaded:
            new_pending = loaded
            new_saved_id = target

    messages = _build_messages(user_text, history, new_pending, summary_content)
    raw = llm_client.chat(messages, 0.7)
    if not raw:
        return "（助手暂时没有响应，请重试。）", new_pending, render_preview(new_pending), new_saved_id

    # 抽取并剥离 scene-spec 代码块
    spec = parse_scene_spec(raw)
    visible = raw
    if spec is not None:
        ok, _ = validate_scene_spec(spec)
        # 只有当用户正在创建新场景或修改现有场景时，才将 scene-spec 作为草稿
        # 如果只是查看场景配置，不应该设置 pending，避免显示预览
        if not is_viewing_scene:
            if ok or spec.get("scenario_type"):
                new_pending = spec
        visible = _SPEC_FENCE_RE.sub("", raw).strip()
    return visible, new_pending, render_preview(new_pending), new_saved_id


# ── SceneAssistant 类（依赖注入，便于测试） ──────────────────────────────────

class SceneAssistant:
    """面向调用方的场景助手封装，支持依赖注入。

    将 LLM 与仓储抽象为参数，便于单测 mock。
    """

    def __init__(self, llm, repo):
        self._llm = llm
        self._repo = repo

    def reply(self, user_text: str, session_id: str = "",
              history: Optional[List[Dict[str, Any]]] = None,
              summary_content: Optional[str] = None) -> str:
        pending_scene = self._repo.find_pending_scene(session_id)
        messages = _build_messages(user_text, history or [], pending_scene,
                                   summary_content)
        return self._llm.chat(messages)


# ── 自检 ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # ponytail: 一个可运行检查 —— 校验 + 解析
    good = {
        "scenario_type": "simple_qa", "name": "1+1 等于几",
        "config": {"agent_roles": {"scheduling_agent": {"role": "调度"},
                                   "execution_agents": [{"name": "数学家", "role": "数学专家"}]},
                   "manual_acceptance": True,
                   "question": "1+1 等于几？", "timeout": 60}}
    ok, err = validate_scene_spec(good)
    assert ok, f"合法草稿应通过校验: {err}"

    # manual_acceptance 非 boolean 应报错
    bad_ma = {**good, "config": {**good["config"], "manual_acceptance": "yes"}}
    ok_ma, err_ma = validate_scene_spec(bad_ma)
    assert not ok_ma and "manual_acceptance" in err_ma, f"非 bool 的 manual_acceptance 应报错: {err_ma}"

    bad = {"scenario_type": "debate", "name": "x", "config": {}}
    ok2, err2 = validate_scene_spec(bad)
    assert not ok2 and "scenario_type" in err2, f"debate 应被拒绝: {err2}"

    bad2 = {"scenario_type": "simple_qa", "name": "x",
            "config": {"agent_roles": {"execution_agents": [{"name": "a", "role": "r"}]}}}
    ok3, err3 = validate_scene_spec(bad2)
    assert not ok3 and "问题" in err3, f"缺 question 应报错: {err3}"

    sample = ("好的，我帮你创建一个问答场景。\n```scene-spec\n"
              + json.dumps(good, ensure_ascii=False) + "\n```\n请确认。")
    parsed = parse_scene_spec(sample)
    assert parsed and parsed["scenario_type"] == "simple_qa", "应从回复中解析出 scene-spec"

    md = render_preview(good)
    assert "✅" in md and "问答" in md, f"预览应含通过标记: {md}"

    # save_scene 路由 + 状态守卫：用桩替代 scenario_manager 与 ScenarioRepository，
    # 避免写入真实数据库。
    calls = {"create": 0, "update": 0}

    class _Stub:
        def create_scenario(self, stype, name, desc, config, created_by=None):
            calls["create"] += 1
            return "new-id-123"

        def update_scenario(self, sid, scenario_type=None, name=None, description=None, config=None):
            assert sid == "new-id-123", "更新应针对已绑定的 scenario_id"
            calls["update"] += 1
            return True

    class _FakeScenario:
        def __init__(self, sid, state):
            self.scenario_id = sid
            self.state = state
            self.scenario_type = "simple_qa"
            self.name = "1+1 等于几"
            self.description = ""
            self.config = json.dumps(good["config"])

    TEST_ID = "abc12345def"  # 仿 UUID hex 片段

    class _StubRepo:
        def find_by_scenario_id(self, sid):
            if sid in ("new-id-123", TEST_ID) or str(sid).startswith(TEST_ID):
                return _FakeScenario(sid if sid != "new-id-123" else TEST_ID, "initializing")
            return None

        def find_all(self):
            return [_FakeScenario(TEST_ID, "initializing")]

    _real_sm = scenario_manager
    _real_repo = ScenarioRepository
    globals()["scenario_manager"] = _Stub()
    globals()["ScenarioRepository"] = _StubRepo
    try:
        sid, err = save_scene(good)
        assert sid == "new-id-123" and not err, f"新建失败: {err}"
        assert calls["create"] == 1 and calls["update"] == 0, "首次保存应走 create"

        sid2, err2 = save_scene({**good, "name": "改"}, scenario_id=sid)
        assert sid2 == sid and not err2, f"更新失败: {err2}"
        assert calls["create"] == 1 and calls["update"] == 1, "二次保存应走 update，而非新建"

        # 状态守卫：非 initializing 不允许修改
        globals()["ScenarioRepository"] = type("_R2", (), {
            "find_by_scenario_id": lambda self, sid: _FakeScenario(sid, "running"),
            "find_all": lambda self: [],
        })
        sid3, err3 = save_scene({**good, "name": "改"}, scenario_id=sid)
        assert sid3 is None and "初始化" in err3, f"running 态应拒绝修改: {err3}"

        # 恢复 initializing 桩，测试修改意图检测
        globals()["ScenarioRepository"] = _StubRepo
        # 纯查询不触发载入
        assert _detect_edit(f"总结场景 {TEST_ID} 的对话", None, None) == (None, None), \
            "查询不应触发载入"
        # 修改关键词 + id 触发载入（返回解析后的完整 id）
        assert _detect_edit(f"修改场景 {TEST_ID} 的问题", None, None)[0] == TEST_ID, \
            "修改应触发载入"
        # 已在编辑同一场景且有草稿：不重复载入（避免丢弃用户改动）
        assert _detect_edit("把问题改成 2+2", TEST_ID, good) == (None, None), \
            "同场景续改不应重载"
    finally:
        globals()["ScenarioRepository"] = _real_repo
        globals()["scenario_manager"] = _real_sm

    print("assistant self-check OK")
