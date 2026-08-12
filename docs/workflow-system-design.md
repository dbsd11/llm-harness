# Workflow 系统设计文档

## 目录

1. [概述](#概述)
2. [数据模型](#数据模型)
3. [Workflow 生命周期](#workflow-生命周期)
4. [场景管理助手集成](#场景管理助手集成)
5. [API 设计](#api-设计)
6. [数据流图](#数据流图)

---

## 概述

Workflow 系统允许将已完成的场景执行结果发布为可重用的工作流定义，支持参数化执行和 DAG（有向无环图）编排。系统由三个核心组件构成：

- **WorkflowPublisher**: 从场景提取工作流定义
- **WorkflowExecutor**: 执行工作流并创建执行场景
- **SceneAssistant**: 提供对话式工作流管理界面

---

## 数据模型

### Workflow 表 (`workflows`)

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 自增主键 |
| `workflow_id` | str | UUID 标识符 |
| `name` | str | 工作流名称 |
| `description` | str | 描述信息 |
| `source_scenario_id` | str | 来源场景 ID（用于追溯和配置继承） |
| `dag_definition` | str (JSON) | DAG 定义：`{"steps": [...]}` |
| `input_schema` | str (JSON) | 输入参数 JSON Schema |
| `agent_roles` | str (JSON) | Agent 角色定义及系统提示词 |
| `experience_context` | str (JSON) | 历史执行经验（每步的状态、结果、错误、建议） |
| `version` | int | 版本号（同一来源场景自动递增） |
| `state` | str | 状态：`draft` / `active` / `archived` |
| `created_by` | str | 创建者标识 |
| `created_at` | datetime | 创建时间 |
| `updated_at` | datetime | 更新时间 |

### WorkflowTaskTemplate 表 (`workflow_task_templates`)

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 自增主键 |
| `template_id` | str | UUID 标识符 |
| `workflow_id` | str | 关联的工作流 ID |
| `step_id` | str | 步骤标识（如 `step_1`, `step_2`） |
| `goal_template` | str | 目标模板（含 `{{param}}` 占位符） |
| `depends_on` | str (JSON) | 依赖的步骤 ID 数组 |
| `agent_role` | str | Agent 角色名称 |
| `system_prompt` | str | Agent 系统提示词 |
| `server_id` | str | 执行服务器 ID（可选） |
| `timeout_seconds` | int | 步骤超时时间（默认 300 秒） |
| `input_param_mapping` | str (JSON) | 模板变量到参数名的映射 |
| `experience_note` | str (JSON) | 历史执行经验 |
| `step_order` | int | 执行顺序 |
| `created_at` | datetime | 创建时间 |
| `updated_at` | datetime | 更新时间 |

---

## Workflow 生命周期

### 1. 创建（发布）

**入口**: `WorkflowPublisher.publish(scenario_id, name, description, created_by)`

**前置条件**:
- 场景必须存在且状态为 `completed` 或 `failed`
- 场景至少有一个终态任务（`success` 或 `failed`）

**提取的数据**:

1. **DAG 结构** (`_build_dag`):
   - 将每个 `task_id` 映射为 `step_id`（如 `step_1`, `step_2`）
   - 执行拓扑排序，将任务分组为执行波次（依赖满足的任务在同一波次，可并行执行）
   - 提取每个任务的：`goal_template`, `depends_on`, `agent_role`, `system_prompt`, `server_id`, `timeout_seconds`

2. **参数提取** (`_extract_parameters`):
   - 扫描场景配置中的字符串值（长度 >= 3）
   - 检查配置值是否出现在任务目标中
   - 如果是，创建参数映射：配置键成为模板变量，目标文本替换为 `{{variable}}` 占位符
   - 生成 `param_map`（参数名 -> 类型/描述/示例）和 `per_step_mapping`（step_id -> {模板变量: 参数名}）

3. **经验收集** (`_collect_experiences`):
   - 每个任务：`step_id`, `task_id`, `state`, `goal`, `result_summary`（结果前 500 字符）, `error`, `review_feedback`, `execution_duration`

4. **输入 Schema** (`_generate_input_schema`):
   - JSON Schema 对象，包含 `type: "object"`, `properties`（来自 param_map）, `required`（所有参数都是必需的）

5. **Agent 角色** (`_extract_agent_roles`):
   - 任务中的唯一 Agent 角色，每个角色包含其 `system_prompt`

**持久化**:
- 创建 `Workflow` 记录，所有提取的数据序列化为 JSON
- 如果同一 `source_scenario_id` 已有工作流，自动递增 `version`
- 通过 `_persist_templates` 创建 `WorkflowTaskTemplate` 记录（每步一个），包含：
  - 带 `{{param}}` 替换的 `goal_template`
  - `depends_on` 为 step_id 的 JSON 数组
  - `input_param_mapping` 链接模板变量到参数名
  - `experience_note` 包含上次执行的状态/结果/错误/建议

**事件**: 发出 `workflow.published`

**返回值**: 包含 `workflow_id`, `name`, `version`, `step_count`, `input_schema`, `dag_definition`, `experience_context` 的字典

### 2. 执行

**入口**: `WorkflowExecutor.execute(workflow_id, input_params, created_by)`

**执行流程**:

1. **验证工作流**: 必须存在且状态为 `active`

2. **验证输入** (`_validate_inputs`): 根据 `input_schema` 检查：
   - 所有 `required` 字段必须存在
   - 类型检查：`string`, `number`, `integer`, `boolean`

3. **加载模板**: `template_repo.find_by_workflow_id()`

4. **检查服务器可用性** (`_check_servers`): 警告离线服务器但不阻塞

5. **加载来源场景配置** (`_load_source_scenario_config`):
   - 从**来源场景**（工作流发布时的场景）读取 `timeout` 和 `manual_acceptance`
   - 每次执行时都读取（不缓存），因此来源场景配置的更新会被反映

6. **创建执行场景** (`_create_execution_scenario`):
   - 创建新的 `Scenario` 记录：
     - `scenario_type = "workflow_execution"`
     - `state = "running"`
     - `config` 包含：`workflow_id`, `input_params`, `is_workflow_execution: true`，以及来自来源场景的 `timeout` 和 `manual_acceptance`
   - 返回新的 `scenario_id`

7. **异步提交执行**: 通过 `ThreadPoolExecutor`（最多 2 个工作线程，前缀 `wf-exec`）

8. **后台执行** (`_run_execution`):
   
   **物化任务** (`_materialize_tasks`):
   - 创建**父任务**（状态 `running`，目标 = `"Workflow execution: {workflow_id}"`）
   - 为每个模板创建**子任务**：
     - 渲染目标（通过 `_render_goal` 替换模板变量）
     - `state = "pending"`
     - `depends_on` 从 step_id 解析为实际 task_id
     - 上下文包含：`role`, `system_prompt`, `question`, `is_workflow_task: true`, `scenario_id`, `experience_context`, `server_id`
   - 返回 `step_id_to_task_id` 映射和 `parent_task_id`

   **构建执行波次** (`_build_waves`): 按依赖对模板进行拓扑排序，检测循环

   **逐波次分发**:
   - 对每个波次：
     - 使用输入参数渲染目标
     - 构建上下文：role, system_prompt, experience_context
     - 注入上游结果 (`_inject_upstream`) - 前一波次的输出成为上下文中的 `upstream_results` 和 `upstream_outputs`
     - 创建 `TaskMessage` 对象并通过 `mqs.dispatch_subtasks()` 分发
     - 通过 `mqs.collect_replies()` 收集回复，超时时间 = 波次模板超时的最大值
     - 跟踪完成/失败计数
     - 波次中失败时：向下游传播失败 (`_propagate_failure`) - 将依赖任务标记为失败，原因 "Skipped: predecessor step failed"

   **最终化**:
   - 全部成功：标记父任务完成，场景状态 -> `completed`，发出 `workflow.execution_completed`
   - 有失败：标记父任务失败，场景状态 -> `failed`，发出 `workflow.execution_failed`
   - 异常：标记父任务失败，场景失败，发出 `workflow.execution_failed`

**事件**: 开始时发出 `workflow.execution_started`，结束时发出 `workflow.execution_completed` 或 `workflow.execution_failed`

**返回值（同步）**: 包含 `scenario_id`, `workflow_id`, `state: "running"`, `total_steps` 的字典

### 3. 更新

**入口**: `PUT /api/workflows/{workflow_id}`

**可更新字段**:
- `name` (字符串)
- `description` (字符串)
- `state` (必须是 `draft`, `active`, `archived` 之一)

**注意**: `updated_at` 时间戳在每次更新时自动设置

### 4. 删除

**入口**: `DELETE /api/workflows/{workflow_id}`

**操作**:
- 先删除所有关联的 `WorkflowTaskTemplate` 记录
- 再删除 `Workflow` 记录

---

## 场景管理助手集成

**文件**: `src/websocket_server/services/scene_assistant.py`

场景助手是一个**对话式 AI 界面**（由 LLM 驱动），通过自然语言提供场景管理和工作流管理功能。

### 工作流相关能力

#### A. 工作流索引与摘要

- `gather_workflow_index()`: 返回所有工作流的文本列表（ID 前缀、名称、状态、版本、步骤数），注入到 LLM 系统提示词的 `[Workflow 清单]` 部分
- `summarize_workflow(workflow_id)`: 生成工作流的详细文本摘要，包括：元数据、输入参数、所有 DAG 步骤（含依赖/服务器分配）、来源场景的执行 Agent 分配。支持前缀匹配。

#### B. 工作流规范解析与验证

- `parse_workflow_spec(text)`: 从 LLM 响应中提取 ` ```workflow-spec {...}` `` ` 围栏代码块
- `validate_workflow_spec(spec)`: 验证结构：
  - `name` 必须非空
  - 至少 1 个步骤
  - 每个步骤需要 `step_id`, `goal_template`, `agent_role`
  - `agent_role` 必须在 `agent_roles` 中定义
  - `depends_on` 引用必须存在
  - 无循环依赖（通过 `_has_cycle` 使用 Kahn 算法检查）
- `check_workflow_server_id_warnings(spec)`: 如果步骤的 `server_id` 与注册的服务器不匹配则警告

#### C. 工作流预览

- `render_workflow_preview(spec)`: 渲染工作流草稿的中文 Markdown 预览，显示：名称、描述、输入参数、Agent 角色、DAG 步骤（含依赖和服务器分配）、验证状态

#### D. 工作流持久化

- `save_workflow(spec, workflow_id=None)`:
  - 如果 `workflow_id` 为 None：创建新的 Workflow + WorkflowTaskTemplate 记录
  - 如果提供了 `workflow_id`：更新现有工作流（版本号递增），删除旧模板，创建新模板
  - 调用 `_create_templates()` 从步骤创建 `WorkflowTaskTemplate` 记录
- `load_workflow_spec(workflow_id)`: 从数据库重建工作流规范字典（支持前缀匹配）

#### E. 编辑检测

- `_detect_workflow_edit(user_text, saved_wf_id, pending_wf)`: 通过查找修改关键词（修改、编辑、调整、更新、变更、重命名、modify、edit、update、rename、change）与工作流相关上下文词的组合，检测用户是否想修改现有工作流

#### F. 系统提示词集成

LLM 系统提示词 (`_SYSTEM_PROMPT`) 包含第 4 和第 5 节，涵盖：
- 第 4 节：管理现有工作流（查看、查询）
- 第 5 节：通过多轮对话创建/修改工作流，使用 `workflow-spec` 预览

系统提示词指示 LLM：
- 创建/编辑工作流时使用 `workflow-spec` 代码块
- 仅查看/查询时使用普通 JSON
- 从可用执行服务器列表中引用服务器 ID
- 基于现有场景创建工作流时，从来源场景的执行 Agent 继承 `server_id`
- 填充 `source_scenario_id` 以保持可追溯性

#### G. 焦点检测

- `_maybe_workflow_focus(user_text)`: 如果用户文本包含工作流关键词 + 十六进制 ID，将完整工作流摘要注入系统提示词的 `[Workflow 聚焦]` 部分

#### H. SceneAssistant 类

- `SceneAssistant.__init__(llm, repo)`: 可依赖注入的包装器
- `SceneAssistant.reply(user_text, session_id, history, summary_content)`: 委托给 `_build_messages` + LLM 聊天

---

## API 设计

**文件**: `src/websocket_server/websocket_server/api_routes_workflow.py`

通过 `register_workflow_routes(app)` 注册。

### 1. 发布工作流

| 项目 | 说明 |
|------|------|
| **方法** | `POST` |
| **路径** | `/api/workflows/publish` |
| **处理器** | `publish_workflow` |
| **请求体** | `{ "scenario_id": str (必需), "name": str (可选), "description": str (可选), "created_by": str (可选) }` |
| **成功响应** | `200 { "success": true, "workflow": { workflow_id, name, version, step_count, input_schema, dag_definition, experience_context } }` |
| **错误响应** | `400` 缺少 scenario_id 或 ValueError；`500` 意外错误 |
| **副作用** | 广播 WebSocket 事件 `"workflow_published"`，包含 `{workflow_id, name}` |

### 2. 列出工作流

| 项目 | 说明 |
|------|------|
| **方法** | `GET` |
| **路径** | `/api/workflows` |
| **处理器** | `list_workflows` |
| **查询参数** | `state` (可选，按状态过滤)；`limit` (可选，默认 100) |
| **成功响应** | `200 { "success": true, "workflows": [workflow_dict, ...], "total": int }` |

每个 `workflow_dict` 包含：`workflow_id`, `name`, `description`, `source_scenario_id`, `dag_definition` (解析的 JSON), `input_schema` (解析的 JSON), `agent_roles` (解析的 JSON), `experience_context` (解析的 JSON), `version`, `state`, `created_by`, `created_at`, `updated_at`。

### 3. 获取工作流（含模板）

| 项目 | 说明 |
|------|------|
| **方法** | `GET` |
| **路径** | `/api/workflows/{workflow_id}` |
| **处理器** | `get_workflow` |
| **路径参数** | `workflow_id` |
| **成功响应** | `200 { "success": true, "workflow": { ...workflow_dict, "templates": [template_dict, ...] } }` |
| **错误响应** | `404` 未找到 |

每个 `template_dict` 包含：`template_id`, `step_id`, `goal_template`, `depends_on` (解析的 JSON 数组), `agent_role`, `server_id`, `timeout_seconds`, `input_param_mapping` (解析的 JSON), `experience_note` (解析的 JSON), `step_order`。

### 4. 更新工作流

| 项目 | 说明 |
|------|------|
| **方法** | `PUT` |
| **路径** | `/api/workflows/{workflow_id}` |
| **处理器** | `update_workflow` |
| **路径参数** | `workflow_id` |
| **请求体** | `{ "name": str (可选), "description": str (可选), "state": "draft"\|"active"\|"archived" (可选) }` |
| **成功响应** | `200 { "success": true, "workflow": workflow_dict }` |
| **错误响应** | `404` 未找到；`400` 无效 JSON |

### 5. 删除工作流

| 项目 | 说明 |
|------|------|
| **方法** | `DELETE` |
| **路径** | `/api/workflows/{workflow_id}` |
| **处理器** | `delete_workflow` |
| **路径参数** | `workflow_id` |
| **成功响应** | `200 { "success": true, "message": "Workflow {id} deleted" }` |
| **错误响应** | `404` 未找到 |
| **副作用** | 先删除所有关联的 `WorkflowTaskTemplate` 记录 |

### 6. 执行工作流

| 项目 | 说明 |
|------|------|
| **方法** | `POST` |
| **路径** | `/api/workflows/{workflow_id}/execute` |
| **处理器** | `execute_workflow` |
| **路径参数** | `workflow_id` |
| **请求体** | `{ "input_params": dict (必需), "created_by": str (可选) }` |
| **成功响应** | `200 { "success": true, "execution": { scenario_id, workflow_id, state: "running", total_steps } }` |
| **错误响应** | `400` 无效 JSON 或 ValueError；`500` 意外错误 |
| **副作用** | 广播 WebSocket 事件 `"workflow_execution_started"`，包含 `{scenario_id, workflow_id}` |

---

## 数据流图

```
场景 (已完成/失败)
    │
    ▼
[WorkflowPublisher.publish()]
    ├── 提取任务，构建 DAG（拓扑排序）
    ├── 提取参数（配置值在目标中出现 -> {{变量}}）
    ├── 收集经验（每个任务的状态、结果、错误、时长）
    ├── 生成 input_schema（JSON Schema）
    ├── 提取 agent_roles（唯一角色 + 系统提示词）
    ├── 持久化：Workflow + WorkflowTaskTemplate 记录
    └── 发出事件：workflow.published
    │
    ▼
Workflow (active) + Templates
    │
    ▼
[WorkflowExecutor.execute()]
    ├── 根据 input_schema 验证输入
    ├── 加载来源场景配置（timeout, manual_acceptance）
    ├── 创建执行场景 (type=workflow_execution)
    ├── 异步提交到 ThreadPoolExecutor
    │       │
    │       ▼
    │   [_run_execution()]
    │       ├── 从模板物化 Task 记录（父任务 + 子任务）
    │       ├── 构建执行波次（拓扑排序）
    │       ├── 对每个波次：
    │       │     ├── 渲染目标（{{param}} -> 实际值）
    │       │     ├── 构建上下文（role, system_prompt, experience, 上游结果）
    │       │     ├── 通过 mqs.dispatch_subtasks() 分发
    │       │     ├── 通过 mqs.collect_replies() 收集回复
    │       │     └── 失败时：向下游依赖传播失败
    │       ├── 最终化：标记父任务 + 场景 完成/失败
    │       └── 发出事件：workflow.execution_completed 或 workflow.execution_failed
    │
    ▼
执行场景 (已完成/失败) 含任务结果
```

### 场景助手交互流

```
用户对话
    │
    ▼
[SceneAssistant.reply()]
    ├── 检测工作流关键词/ID
    ├── 注入工作流索引/摘要到系统提示词
    ├── LLM 生成响应
    │       │
    │       ├── 包含 workflow-spec 代码块？
    │       │     ├── 解析规范
    │       │     ├── 验证结构
    │       │     ├── 渲染预览
    │       │     └── 等待用户确认
    │       │
    │       └── 用户确认保存？
    │             ├── save_workflow(spec)
    │             └── 持久化到数据库
    │
    ▼
返回响应给用户
```

---

## 关键设计决策

### 1. 来源场景配置继承

工作流执行时从来源场景动态加载 `timeout` 和 `manual_acceptance` 配置，而不是在发布时缓存。这确保：
- 来源场景配置的更新会反映在新的工作流执行中
- 工作流定义保持轻量，不复制配置数据
- 配置变更无需重新发布工作流

### 2. 场景复用

工作流执行创建新的场景（`scenario_type="workflow_execution"`），复用现有的场景执行链路：
- 统一的状态管理和生命周期跟踪
- 复用场景 API 查看执行进度和结果
- 避免维护两套独立的任务执行系统

### 3. 波次并行执行

通过拓扑排序将任务分组为波次：
- 同一波次内的任务可并行执行
- 波次间按依赖顺序串行执行
- 失败向下游传播，跳过依赖失败步骤的任务

### 4. 经验上下文

每次执行的经验（状态、结果摘要、错误、建议）被保存并注入后续执行：
- 帮助执行 Agent 避免重复错误
- 提供历史指导，提高成功率
- 形成持续学习的闭环

### 5. 对话式管理

通过场景助手提供自然语言界面：
- 降低使用门槛，无需记忆 API
- 支持多轮对话逐步完善工作流定义
- 实时预览和验证，即时反馈

---

## 错误处理

### 发布阶段

- 场景不存在或状态非终态：返回 400 错误
- 场景无任务：返回 400 错误
- DAG 循环依赖：返回 400 错误（通过拓扑排序检测）

### 执行阶段

- 工作流不存在或非 active：返回 400 错误
- 输入验证失败：返回 400 错误，详细说明缺失/类型错误的字段
- 服务器离线：记录警告但不阻塞执行
- 任务执行失败：标记场景为 failed，向下游传播失败
- 超时：根据 `timeout_seconds` 设置，超时后标记任务失败

### 助手交互

- 规范验证失败：返回详细错误信息，指出具体问题
- 服务器 ID 不匹配：发出警告但允许保存
- 循环依赖：阻止保存，要求修正

---

## 扩展点

### 当前限制

1. **最大迭代次数**: 执行 Agent 的 ReAct 循环限制为 10 次迭代
2. **超时**: 默认步骤超时 300 秒，可通过模板覆盖
3. **并行度**: ThreadPoolExecutor 最多 2 个工作线程

### 未来增强

1. **条件分支**: 支持基于执行结果的动态路由
2. **人工审批节点**: 在工作流中插入人工审批步骤
3. **子工作流**: 允许工作流调用其他工作流
4. **定时触发**: 支持 cron 表达式定时执行
5. **Webhook 回调**: 执行完成后回调指定 URL
6. **版本管理**: 支持工作流版本回滚
7. **权限控制**: 基于用户/角色的工作流访问控制

---

## 附录

### 相关文件清单

| 文件 | 路径 | 说明 |
|------|------|------|
| Workflow 模型 | `src/websocket_server/database/models/workflow.py` | 数据模型定义 |
| WorkflowTaskTemplate 模型 | `src/websocket_server/database/models/workflow_task_template.py` | 任务模板模型 |
| Workflow 仓库 | `src/websocket_server/database/repositories/workflow_repository.py` | 数据访问层 |
| WorkflowTaskTemplate 仓库 | `src/websocket_server/database/repositories/workflow_task_template_repository.py` | 模板数据访问 |
| Workflow 发布器 | `src/websocket_server/services/workflow_publisher.py` | 场景到工作流转换 |
| Workflow 执行器 | `src/websocket_server/services/workflow_executor.py` | 工作流执行引擎 |
| 场景助手 | `src/websocket_server/services/scene_assistant.py` | 对话式管理界面 |
| API 路由 | `src/websocket_server/websocket_server/api_routes_workflow.py` | REST API 端点 |
| 消息队列 | `src/websocket_server/core/message_queue.py` | 任务分发和回复收集 |

### 事件清单

| 事件 | 触发时机 | 数据 |
|------|----------|------|
| `workflow.published` | 工作流发布成功 | `{workflow_id, name, version}` |
| `workflow.execution_started` | 工作流执行开始 | `{scenario_id, workflow_id, step_count}` |
| `workflow.execution_completed` | 工作流执行成功 | `{scenario_id, workflow_id, completed_steps}` |
| `workflow.execution_failed` | 工作流执行失败 | `{scenario_id, workflow_id, error}` |
| `task.skipped` | 任务因前驱失败被跳过 | `{task_id, reason}` |

---

**文档版本**: 1.0  
**最后更新**: 2026-08-12  
**维护者**: LLM Harness Team
