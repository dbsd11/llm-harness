# ASP 场景助手 — 创建场景流程设计文档

## 1. 系统架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│  Gradio UI (pages/home/__init__.py)                             │
│  ┌──────────┐  ┌──────────────┐  ┌────────────────────────┐    │
│  │ Chatbot  │  │ Preview 面板  │  │ 确认保存 / 放弃草稿按钮 │    │
│  └────┬─────┘  └──────▲───────┘  └───────────▲────────────┘    │
│       │               │                       │                 │
└───────┼───────────────┼───────────────────────┼─────────────────┘
        │               │                       │
        ▼               │                       │
┌───────────────────────┤                       │
│  api_client.chat()    │                       │
│  POST /api/chat       │                       │
└───────┬───────────────┘                       │
        │                                       │
        ▼                                       │
┌───────────────────────────────────────────────────────────────────┐
│  WS Server (websocket_server)                                     │
│  /api/chat handler → 调用 assistant.reply() 逻辑                  │
│  ┌─────────────────────────────────────────────────────────┐      │
│  │  assistant.py — 场景管理助手核心逻辑                       │      │
│  │                                                         │      │
│  │  ┌─────────────┐   ┌────────────┐   ┌──────────────┐  │      │
│  │  │ System      │   │ ReAct Loop │   │ scene-spec   │  │      │
│  │  │ Prompt +    │──▶│ (tool调用)  │──▶│ 解析/校验    │  │      │
│  │  │ Tools       │   │            │   │              │  │      │
│  │  └─────────────┘   └─────┬──────┘   └──────┬───────┘  │      │
│  └──────────────────────────┼─────────────────┼───────────┘      │
│                             │                 │                   │
│                    ┌────────▼──────┐  ┌───────▼──────────┐       │
│                    │ Tool Handlers │  │ save_scene()     │       │
│                    │ - list_exec   │  │ → scenario_mgr   │       │
│                    │ - list_human  │  │   .create_scenario│       │
│                    │ - list_scenes │  │                  │       │
│                    └───────┬───────┘  └───────┬──────────┘       │
│                            │                  │                   │
└────────────────────────────┼──────────────────┼───────────────────┘
                             │                  │
                    ┌────────▼──────┐  ┌────────▼──────────┐
                    │ WS Hub API    │  │ ASP Local DB      │
                    │ /api/servers  │  │ (SQLite)          │
                    │ (JWT租户隔离)  │  │ scenarios 表       │
                    └───────────────┘  └───────────────────┘
```

## 2. 创建场景完整流程

### 2.1 流程时序图

```
用户                Gradio UI           api_client         WS Server          assistant.py         LLM (Qwen)        WS Hub API       ASP DB
 │                    │                    │                   │                   │                   │                 │                │
 │  "帮我创建一个      │                    │                   │                   │                   │                 │                │
 │   问答场景"         │                    │                   │                   │                   │                 │                │
 │───────────────────▶│                    │                   │                   │                   │                 │                │
 │                    │  POST /api/chat    │                   │                   │                   │                 │                │
 │                    │───────────────────▶│                   │                   │                   │                 │                │
 │                    │                    │  /api/chat        │                   │                   │                 │                │
 │                    │                    │──────────────────▶│                   │                   │                 │                │
 │                    │                    │                   │  reply()          │                   │                 │                │
 │                    │                    │                   │──────────────────▶│                   │                 │                │
 │                    │                    │                   │                   │  _detect_edit()   │                 │                │
 │                    │                    │                   │                   │──┐                │                 │                │
 │                    │                    │                   │                   │  │ 无修改意图      │                 │                │
 │                    │                    │                   │                   │◀─┘                │                 │                │
 │                    │                    │                   │                   │                   │                 │                │
 │                    │                    │                   │                   │  _build_messages() │                │                │
 │                    │                    │                   │                   │  (system prompt +  │                │                │
 │                    │                    │                   │                   │   history + user)  │                │                │
 │                    │                    │                   │                   │─────────┐         │                 │                │
 │                    │                    │                   │                   │         │         │                 │                │
 │                    │                    │                   │                   │  _react_loop()    │                 │                │
 │                    │                    │                   │                   │─────────┐         │                 │                │
 │                    │                    │                   │                   │         │         │                 │                │
 │                    │                    │                   │                   │    chat_with_tools │                │                │
 │                    │                    │                   │                   │         │────────▶│                 │                │
 │                    │                    │                   │                   │         │         │                 │                │
 │                    │                    │                   │                   │         │  tool_call:              │                │
 │                    │                    │                   │                   │         │  list_execution_servers  │                │
 │                    │                    │                   │                   │         │◀────────│                 │                │
 │                    │                    │                   │                   │         │         │                 │                │
 │                    │                    │                   │                   │  _dispatch_tool_call()              │                │
 │                    │                    │                   │                   │─────────┐         │                 │                │
 │                    │                    │                   │                   │         │         │                 │                │
 │                    │                    │                   │                   │  GET /api/servers (Bearer JWT)       │                │
 │                    │                    │                   │                   │         │─────────────────────────▶│                │
 │                    │                    │                   │                   │         │         │  tenant_id过滤  │                │
 │                    │                    │                   │                   │         │◀────────────────────────│                │
 │                    │                    │                   │                   │         │         │                 │                │
 │                    │                    │                   │                   │  返回server列表JSON │                │                │
 │                    │                    │                   │                   │◀────────┘         │                 │                │
 │                    │                    │                   │                   │         │         │                 │                │
 │                    │                    │                   │                   │    第2轮 chat_with_tools            │                │
 │                    │                    │                   │                   │         │────────▶│                 │                │
 │                    │                    │                   │                   │         │         │                 │                │
 │                    │                    │                   │                   │         │  最终回复 +              │                │
 │                    │                    │                   │                   │         │  ```scene-spec```        │                │
 │                    │                    │                   │                   │         │◀────────│                 │                │
 │                    │                    │                   │                   │◀────────┘         │                 │                │
 │                    │                    │                   │                   │                   │                 │                │
 │                    │                    │                   │                   │  parse_scene_spec()                │                │
 │                    │                    │                   │                   │  validate_scene_spec()             │                │
 │                    │                    │                   │                   │  check_server_id_warnings()        │                │
 │                    │                    │                   │                   │  render_preview() │                │                │
 │                    │                    │                   │                   │─────────┐         │                 │                │
 │                    │                    │                   │                   │         │         │                 │                │
 │                    │                    │                   │  resp: reply +    │         │         │                 │                │
 │                    │                    │                   │  pending_scene    │         │         │                 │                │
 │                    │                    │                   │◀──────────────────│◀────────┘         │                 │                │
 │                    │  chatbot + preview │                   │                   │                   │                 │                │
 │                    │◀──────────────────│◀──────────────────│                   │                   │                 │                │
 │  看到回复 + 预览    │                    │                   │                   │                   │                 │                │
 │◀──────────────────│                    │                   │                   │                   │                 │                │
 │                    │                    │                   │                   │                   │                 │                │
 │  点击"确认保存"     │                    │                   │                   │                   │                 │                │
 │───────────────────▶│                    │                   │                   │                   │                 │                │
 │                    │  POST /api/chat/save                   │                   │                   │                 │                │
 │                    │───────────────────▶│                   │                   │                   │                 │                │
 │                    │                    │──────────────────▶│  save_scene()     │                   │                 │                │
 │                    │                    │                   │──────────────────▶│                   │                 │                │
 │                    │                    │                   │                   │  validate ──▶ OK  │                 │                │
 │                    │                    │                   │                   │  scenario_manager  │                 │                │
 │                    │                    │                   │                   │  .create_scenario()│                │                │
 │                    │                    │                   │                   │───────────────────────────────────────────────────▶│
 │                    │                    │                   │                   │                   │                 │  INSERT scenario│
 │                    │                    │                   │                   │                   │                 │  (tenant_id自动)│
 │                    │                    │                   │                   │◀───────────────────────────────────────────────────│
 │                    │                    │                   │  scenario_id      │                   │                 │                │
 │                    │                    │                   │◀──────────────────│                   │                 │                │
 │                    │  保存成功提示       │◀──────────────────│                   │                   │                 │                │
 │                    │◀──────────────────│                    │                   │                   │                 │                │
```

### 2.2 分步说明

| 步骤 | 组件 | 动作 | 说明 |
|------|------|------|------|
| 1 | Gradio UI | 用户输入文本 | 如"帮我创建一个问答场景" |
| 2 | Gradio → api_client | `on_send()` → `api_client.chat()` | POST 到 WS Server `/api/chat` |
| 3 | WS Server | 路由到 chat handler | 调用 `assistant.reply()` 逻辑 |
| 4 | assistant.py | `_detect_edit()` | 检测是否修改已有场景（关键词 + ID 匹配） |
| 5 | assistant.py | `_build_messages()` | 组装 system prompt + 历史消息 + 当前草稿 |
| 6 | assistant.py | `_react_loop()` | ReAct 循环，最多 5 轮 |
| 7 | assistant.py → LLM | `chat_with_tools(messages, tools)` | LLM 决定是否调用 tool |
| 8 | assistant.py | `_dispatch_tool_call()` | 执行 tool handler（如 `list_execution_servers`） |
| 9 | tool handler → WS Hub | `GET /api/servers` (Bearer JWT) | WS Hub 按 JWT tenantId 过滤返回 |
| 10 | assistant.py → LLM | 追加 tool 结果，再次调用 | LLM 基于真实数据生成 scene-spec |
| 11 | assistant.py | `parse_scene_spec()` | 从 LLM 回复中提取 ` ```scene-spec` 代码块 |
| 12 | assistant.py | `validate_scene_spec()` | 校验结构、必填字段、类型合法性 |
| 13 | assistant.py | `check_server_id_warnings()` | 校验 server_id 是否存在于已知服务器 |
| 14 | assistant.py | `render_preview()` | 生成 Markdown 预览（含校验状态） |
| 15 | Gradio UI | 显示回复 + 预览面板 | 用户看到对话回复和草稿预览 |
| 16 | 用户 | 多轮对话调整 | 可继续修改，每轮更新 scene-spec |
| 17 | 用户 | 点击"确认保存到数据库" | `on_confirm()` → `api_client.chat_save()` |
| 18 | assistant.py | `save_scene()` | 最终校验 → `scenario_manager.create_scenario()` |
| 19 | scenario_manager | DB INSERT | 写入 scenarios 表，自动注入 tenant_id |

## 3. scene-spec 数据结构

LLM 在回复中附带 ` ```scene-spec` 代码块，格式如下：

```json
{
  "scenario_type": "simple_qa | code_execution",
  "name": "场景名称",
  "description": "可选描述",
  "config": {
    "agent_roles": {
      "scheduling_agent": {"role": "调度角色描述"},
      "execution_agents": [
        {
          "name": "Agent名称",
          "role": "角色/专长",
          "server_id": "从 list_execution_servers 工具返回的 ID"
        }
      ]
    },
    "manual_acceptance": false,
    "question": "问答场景的问题",
    "timeout": 3600
  }
}
```

### 3.1 场景类型及必填字段

| 类型 | scenario_type | config 必填字段 | 说明 |
|------|--------------|----------------|------|
| 问答 | `simple_qa` | `question` | 向 Agent 提出问题 |
| 代码执行 | `code_execution` | `script` 或 `code` | 执行 Python 脚本/代码 |

### 3.2 校验规则 (`validate_scene_spec`)

| 校验项 | 规则 | 错误提示 |
|--------|------|----------|
| scenario_type | 必须是 `simple_qa` 或 `code_execution` | "scenario_type 必须是 (...) 之一" |
| name | 非空字符串 | "场景名称(name)不能为空" |
| config | 必须是 dict | "config 必须是对象" |
| execution_agents | 至少 1 个含 name + role 的 Agent | "至少需要一个执行 Agent" |
| manual_acceptance | 若存在，必须是 boolean | "manual_acceptance 必须是 true 或 false" |
| simple_qa.question | 非空 | "问答场景需要 问题" |
| code_execution.script/code | 至少有一个 | "代码执行场景需要 Python 脚本或代码片段" |

### 3.3 server_id 校验 (`check_server_id_warnings`)

```
spec 中的 server_id 集合
        │
        ▼
  get_known_server_ids()
  (调用 list_execution_servers + list_human_servers tool handlers)
        │
        ▼
  差集 = spec_ids - known_ids
        │
   ┌────┴────┐
   │ 差集非空  │ → 返回警告："以下 server_id 不存在于已注册服务器中"
   └────┬────┘
        │ 差集为空
        ▼
   无警告，校验通过
```

## 4. Tool 系统

### 4.1 Tool 定义

| Tool 名称 | 功能 | 数据来源 | 租户隔离 |
|-----------|------|----------|----------|
| `list_execution_servers` | 查询可用执行服务器 | WS Hub `GET /api/servers` → 过滤 `source != "human_agent"` | WS Hub JWT tenantId 过滤 |
| `list_human_servers` | 查询可用人工作业服务器 | WS Hub `GET /api/servers` → 过滤 `source == "human_agent"` | WS Hub JWT tenantId 过滤 |
| `list_scenarios` | 查询当前场景列表 | ASP 本地 DB `ScenarioRepository.find_all()` | `base_repository` 严格租户模式 |

### 4.2 ReAct Loop

```
┌──────────────────────────────────────────────┐
│  _react_loop(messages, tools)                │
│                                              │
│  for i in range(5):  # _MAX_REACT_ITERATIONS │
│    ┌─────────────────────────────────────┐   │
│    │ response = chat_with_tools(msg, tools)│  │
│    └──────────────┬──────────────────────┘   │
│                   │                          │
│          ┌────────┴────────┐                 │
│          │                 │                 │
│    无 tool_calls      有 tool_calls          │
│          │                 │                 │
│          ▼                 ▼                 │
│    return content    执行每个 tool call       │
│    (最终回复)        追加 tool 结果到 messages │
│                          │                   │
│                          ▼                   │
│                    继续下一轮循环              │
│                                              │
│  超过 5 轮 → 返回 "达到最大工具调用轮次"       │
└──────────────────────────────────────────────┘
```

### 4.3 租户隔离链路

```
ASP (tenant JWT: ea44f31b-...)
    │
    │  tool handler: _handle_list_execution_servers()
    │  → list_servers() → GET /api/servers (Bearer JWT)
    │
    ▼
WS Hub (auth_middleware)
    │
    │  解析 JWT → tenant_id = "ea44f31b-..."
    │  → find_all_by_tenant("ea44f31b-...")
    │  → WS_TENANT_STRICT=true → WHERE tenant_id = "ea44f31b-..."
    │
    ▼
返回结果：仅包含 tenant_id = "ea44f31b-..." 的服务器
```

## 5. 状态管理

### 5.1 Gradio State 组件

| State | 类型 | 说明 |
|-------|------|------|
| `pending_state` | `dict \| None` | 当前 scene-spec 草稿 |
| `saved_id_state` | `str \| None` | 已保存场景的 scenario_id（修改模式） |
| `dirty_state` | `bool` | 草稿是否有未保存变更 |
| `preview_md` | `str` | 预览面板 Markdown 文本 |
| `draft_actions` | `gr.Row` | 确认/放弃按钮可见性 |

### 5.2 草稿生命周期

```
用户开始对话
    │
    ▼
pending = None, saved_id = None
    │
    ▼  (LLM 回复含 scene-spec)
pending = {spec}, dirty = True
preview 显示校验结果
    │
    ├── 用户继续修改 → pending 更新，dirty = True
    │
    ├── 用户点击"确认保存"
    │       │
    │       ▼
    │   save_scene(pending, saved_id)
    │       │
    │       ├── saved_id 为空 → create_scenario() → 返回新 ID
    │       └── saved_id 非空 → update_scenario() → 更新已有记录
    │       │
    │       ▼
    │   dirty = False, preview 显示"已保存"
    │
    └── 用户点击"放弃草稿"
            │
            ▼
        pending = None, dirty = False, saved_id = None
        preview 恢复默认文本
```

### 5.3 修改已有场景

```
用户输入: "修改场景 abc12345 的问题"
    │
    ▼
_detect_edit() 检测:
    ├── 含修改关键词 ("修改")
    ├── 提取 ID ("abc12345")
    ├── 查找场景 → 存在
    └── 检查状态 → "initializing" ?
            │
        ┌───┴───┐
        │       │
     是 initializing  非 initializing
        │       │
        ▼       ▼
  载入全量配置   返回错误:
  到 pending     "仅 initializing 状态可修改"
        │
        ▼
  LLM 基于全量草稿修改
  输出完整 scene-spec
```

## 6. 落库流程

### 6.1 新建场景 (`save_scene` → `create_scenario`)

```python
scenario_manager.create_scenario(
    scenario_type=spec["scenario_type"],   # "simple_qa" | "code_execution"
    name=spec["name"],
    description=spec.get("description", ""),
    config=spec.get("config", {}),
)
```

内部逻辑:
1. 生成 `scenario_id = uuid4()`
2. 生成 `trace_id = uuid4()`
3. 创建 `Scenario` 对象，`state = "initializing"`
4. `base_repository.create()` 自动注入 `tenant_id` (从 JWT)
5. INSERT 到 `scenarios` 表
6. 触发 `event_bus.emit("scenario.created", ...)`
7. 返回 `scenario_id`

### 6.2 更新场景 (`save_scene` → `update_scenario`)

前置条件:
- `scenario_id` 非空
- 场景状态为 `"initializing"`

```python
scenario_manager.update_scenario(
    scenario_id,
    scenario_type=spec["scenario_type"],
    name=spec["name"],
    description=spec.get("description", ""),
    config=spec.get("config", {}),
)
```

## 7. System Prompt 关键约束

### 7.1 工具调用规则

```
1. 回答场景问题前，必须先调用 list_scenarios
2. 创建/修改场景时，必须先调用 list_execution_servers
3. server_id 只能从工具返回结果选取，严禁编造
4. 工具返回空列表时，告知用户无可用服务器
```

### 7.2 scene-spec 输出规则

- 创建或修改场景时，回复末尾必须附完整 `scene-spec` 代码块
- 纯查询/查看场景时，使用普通 JSON，不使用 `scene-spec`
- 草稿每轮更新为全量配置，不做增量

## 8. 关键文件索引

| 文件 | 职责 |
|------|------|
| `pages/home/assistant.py` | 场景助手核心逻辑：prompt、tool、ReAct loop、校验、保存 |
| `pages/home/__init__.py` | Gradio UI：chatbot、preview、confirm/discard 按钮 |
| `scenarios/local_scenario_manager.py` | 场景生命周期管理：create/update |
| `database/repositories/scenario_repository.py` | 场景 DB 操作 |
| `database/repositories/base_repository.py` | 基础仓储：租户过滤、自动注入 tenant_id |
| `core/local_websocket_api_client.py` | WS Hub REST API 客户端 |
| `core/local_llm_client.py` | LLM 客户端（chat_with_tools） |
| `core/local_tenant.py` | 租户上下文：从 JWT 提取 tenantId |
| `api_client.py` | Gradio → WS Server HTTP 客户端 |
