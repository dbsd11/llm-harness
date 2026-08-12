# WebSocket Server API Key 鉴权与租户隔离接口设计文档

## 目录

1. [概述](#概述)
2. [API Key 格式规范](#api-key-格式规范)
3. [鉴权机制](#鉴权机制)
4. [租户隔离机制](#租户隔离机制)
5. [接口清单与鉴权/隔离矩阵](#接口清单与鉴权隔离矩阵)
6. [接口详细设计](#接口详细设计)
7. [WebSocket 连接鉴权](#websocket-连接鉴权)
8. [实时事件广播隔离](#实时事件广播隔离)
9. [错误响应规范](#错误响应规范)
10. [配置参考](#配置参考)
11. [数据迁移](#数据迁移)

---

## 概述

WebSocket Server（以下简称 `ws_server`）通过 JWT API Key 实现无状态鉴权，并在 REST API、WebSocket 连接、数据库查询三个层面实施租户隔离。

核心设计原则：

- **无状态鉴权**：ws_server 独立验证 JWT 签名，不依赖外部会话存储
- **零依赖实现**：HS256 签名验证使用 Python 标准库（`hmac` + `hashlib`），无需 PyJWT 或 cryptography
- **全链路租户隔离**：`tenant_id` 从 JWT 提取后，贯穿 REST 请求、WS 连接、DB 查询、事件广播
- **防枚举设计**：资源所有权校验失败统一返回 `404`（而非 `403`），防止资源存在性泄露

---

## API Key 格式规范

### JWT 结构

API Key 为标准三段式 JWT：`headerB64.payloadB64.signature`

**Header**（解码后）：
```json
{
  "alg": "HS256",
  "typ": "JWT"
}
```

**Payload**（解码后）：
```json
{
  "tenantId": "c928e08e-1234-5678-9abc-def012345678",
  "iat": 1770610935,
  "exp": 1802146935
}
```

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `tenantId` | string (UUID) | 是 | 租户唯一标识 |
| `iat` | number | 否 | 签发时间戳（秒） |
| `exp` | number | 否 | 过期时间戳（秒）。若存在且已过期，返回 `401 Token expired` |

**签名算法**：HMAC-SHA256，密钥由 `WS_JWT_SECRET` 环境变量指定。

### 签名验证流程

```
输入: api_key (JWT 字符串), secret (WS_JWT_SECRET)
  │
  ├─ 切分为 3 段 ──────────── 失败 → InvalidTokenError
  │
  ├─ 计算 HMAC-SHA256("{header}.{payload}", secret)
  │  └─ base64url 编码得到 expected_signature
  │
  ├─ hmac.compare_digest(actual_sig, expected_sig) ── 不匹配 → InvalidTokenError
  │
  ├─ base64url 解码 payload → JSON.parse
  │  └─ 解析失败 → InvalidTokenError
  │
  ├─ 检查 exp: now > exp ──── 已过期 → TokenExpiredError
  │
  ├─ 检查 tenantId 存在 ──── 缺失 → MissingTenantError
  │
  └─ 返回 payload dict（含 tenantId, iat, exp）
```

关键安全特性：
- **恒定时间比较**：使用 `hmac.compare_digest()` 防止时序攻击
- **Base64URL 编码**：签名使用 URL 安全字符集（`-` 替代 `+`，`_` 替代 `/`，无 padding）

---

## 鉴权机制

### REST API 鉴权

由 `auth/middleware.py` 实现的 aiohttp 中间件，拦截所有请求：

```
请求进入
  │
  ├─ 路径 ∈ 白名单 (/api/health) ──→ 直接放行
  │
  ├─ 路径 ∉ /api/* ──────────────→ 直接放行（静态文件等）
  │
  ├─ 提取 Token（按优先级）:
  │   1. Authorization: Bearer <token>
  │   2. X-API-Key: <token>
  │   └─ 均未找到 → 401 "Missing API key"
  │
  ├─ verify_and_parse_api_key(token, secret)
  │   ├─ TokenExpiredError  → 401 "Token expired"
  │   ├─ InvalidTokenError  → 401 "Invalid token"
  │   └─ AuthError          → 401 "{error message}"
  │
  ├─ 注入 request["tenant_id"] = payload["tenantId"]
  │
  └─ 调用 handler
```

**白名单路由**（免鉴权）：

| 路径 | 说明 |
|------|------|
| `/api/health` | 健康检查，用于负载均衡探针 |

### WebSocket 鉴权

由 `auth/ws_auth.py` 在 WS 握手阶段执行，适用于 `GET /`（Agent 连接）和 `GET /subscribe`（事件订阅）：

**Token 提取优先级**：

| 优先级 | 方式 | 格式 |
|--------|------|------|
| 1 | URL 查询参数 | `?api_key=<JWT>` |
| 2 | HTTP Header | `Authorization: Bearer <JWT>` |
| 3 | WS Subprotocol | `Sec-WebSocket-Protocol: api-key.<JWT>` |

**处理结果**：
- 成功：返回 `tenant_id` 字符串，连接继续
- 失败：返回 `None`，HTTP 响应 `401 Unauthorized`，连接不建立

### 鉴权错误响应

所有鉴权失败统一返回 HTTP 401：

```json
{
  "success": false,
  "error": "Unauthorized: Missing API key"
}
```

```json
{
  "success": false,
  "error": "Unauthorized: Token expired"
}
```

```json
{
  "success": false,
  "error": "Unauthorized: Invalid token"
}
```

---

## 租户隔离机制

### 隔离层次

```
┌──────────────────────────────────────────────────────────────┐
│  Layer 1: REST API 中间件                                     │
│  auth_middleware → request["tenant_id"] = payload["tenantId"] │
├──────────────────────────────────────────────────────────────┤
│  Layer 2: API Handler 层                                      │
│  ├─ 列表查询: repo.find_all_by_tenant(tenant_id)              │
│  ├─ 单资源操作: ownership check → 404 if mismatch             │
│  └─ 创建操作: 设置 resource.tenant_id = tenant_id             │
├──────────────────────────────────────────────────────────────┤
│  Layer 3: Repository / DB 层                                  │
│  _tenant_query(): WHERE tenant_id = ? [OR tenant_id IS NULL] │
├──────────────────────────────────────────────────────────────┤
│  Layer 4: WebSocket 事件广播                                   │
│  _broadcast_worker: skip if event_tenant != subscriber_tenant │
└──────────────────────────────────────────────────────────────┘
```

### 数据模型

所有 12 张表均包含 `tenant_id TEXT` 列及索引：

| 表名 | 索引名 | 说明 |
|------|--------|------|
| `users` | `idx_users_tenant` | 用户账户 |
| `agents` | `idx_agents_tenant` | Agent 实例 |
| `scenarios` | `idx_scenarios_tenant` | 场景定义 |
| `tasks` | `idx_tasks_tenant` | 执行任务 |
| `events` | `idx_events_tenant` | 系统事件 |
| `messages` | `idx_messages_tenant` | Agent 间消息 |
| `consumer_offsets` | `idx_consumer_offsets_tenant` | 事件消费位点 |
| `execution_servers` | `idx_execution_servers_tenant` | 执行服务器 |
| `human_tasks` | `idx_human_tasks_tenant` | 人工任务 |
| `assistant_messages` | `idx_assistant_messages_tenant` | 助手聊天记录 |
| `workflows` | `idx_workflows_tenant` | 工作流定义 |
| `workflow_task_templates` | `idx_workflow_task_templates_tenant` | 工作流步骤模板 |

### 两种隔离模式

由 `WS_TENANT_STRICT` 环境变量控制：

**兼容模式**（`WS_TENANT_STRICT=false`，默认）：
```sql
WHERE tenant_id = ? OR tenant_id IS NULL
```
- `tenant_id` 为 NULL 的行对所有租户可见
- 适用于迁移过渡期，已有数据无需立即回填

**严格模式**（`WS_TENANT_STRICT=true`）：
```sql
WHERE tenant_id = ?
```
- 仅返回精确匹配的行
- 生产环境推荐使用

### 租户隔离模式详解

#### A. 列表查询隔离（Query Isolation）

所有列表接口根据 `tenant_id` 过滤返回结果：

```python
# 优先级链：
if specific_filter:
    repo.find_by_xxx(value, tenant_id=tenant_id)
elif tenant_id:
    repo.find_all_by_tenant(tenant_id)
else:
    repo.find_all()
```

#### B. 单资源所有权校验（Ownership Check）

对单个资源的操作（读取、修改、删除、执行），统一使用所有权校验：

```python
resource = repo.find_by_id(resource_id)
if not resource:
    return 404, {"success": false, "error": "Xxx not found"}

# 所有权校验：tenant 不匹配时返回 404（而非 403，防枚举）
if tenant_id and resource.tenant_id and resource.tenant_id != tenant_id:
    return 404, {"success": false, "error": "Xxx not found"}
```

#### C. 创建时绑定租户（Tenant Binding on Create）

创建类接口在资源创建后，将 `tenant_id` 写入资源记录：

```python
resource = create_xxx(...)
repo.update_tenant_id(resource.id, tenant_id)
```

#### D. 关联操作校验（Cross-Reference Validation）

涉及跨资源操作时，校验关联资源的租户归属：

```python
# 任务派发：校验目标服务器属于当前租户
server = repo.get_server(target_server_id)
if tenant_id and server.tenant_id and server.tenant_id != tenant_id:
    return 404, {"success": false, "error": "Server not found"}
```

---

## 接口清单与鉴权/隔离矩阵

### 服务管理（Server Management）

| Method | Path | 鉴权 | 租户隔离 | 隔离方式 |
|--------|------|------|----------|----------|
| GET | `/api/health` | 免鉴权 | 无 | — |
| GET | `/api/servers` | 必需 | 是 | 列表过滤 |
| GET | `/api/servers/{server_id}` | 必需 | 是 | 所有权校验 |
| POST | `/api/servers/offline` | 必需 | 是 | 租户范围删除 |
| DELETE | `/api/servers/{server_id}` | 必需 | 是 | 所有权校验 |
| POST | `/api/tasks/dispatch` | 必需 | 是 | 服务器归属校验 + 消息绑定 |

### 场景管理（Scenario Management）

| Method | Path | 鉴权 | 租户隔离 | 隔离方式 |
|--------|------|------|----------|----------|
| POST | `/api/scenarios` | 必需 | 是 | 创建时绑定 |
| GET | `/api/scenarios` | 必需 | 是 | 列表过滤 |
| GET | `/api/scenarios/{scenario_id}` | 必需 | 是 | 所有权校验 |
| GET | `/api/scenarios/{scenario_id}/messages` | 必需 | 是 | 所有权校验 |
| POST | `/api/scenarios/{scenario_id}/start` | 必需 | 是 | 所有权校验 |
| POST | `/api/scenarios/{scenario_id}/stop` | 必需 | 是 | 所有权校验 |
| DELETE | `/api/scenarios/{scenario_id}` | 必需 | 是 | 所有权校验 |

### 任务管理（Task Management）

| Method | Path | 鉴权 | 租户隔离 | 隔离方式 |
|--------|------|------|----------|----------|
| GET | `/api/tasks` | 必需 | 是 | 列表过滤 |
| GET | `/api/tasks/{task_id}` | 必需 | 是 | 所有权校验 |
| DELETE | `/api/tasks/{task_id}` | 必需 | 是 | 所有权校验 |
| POST | `/api/tasks/{task_id}/accept` | 必需 | 是 | 所有权校验 |

### 聊天 / 场景助手（Chat / Scene Assistant）

| Method | Path | 鉴权 | 租户隔离 | 隔离方式 |
|--------|------|------|----------|----------|
| POST | `/api/chat` | 必需 | 否 | 基于 session，非租户范围 |
| POST | `/api/chat/save` | 必需 | 是 | 保存时绑定 tenant_id |
| POST | `/api/chat/workflow/save` | 必需 | 是 | 保存时绑定 tenant_id |
| GET | `/api/chat/preview` | 必需 | 否 | 预览功能，无持久化 |
| GET | `/api/chat/load-scene` | 必需 | 是 | 所有权校验 |
| GET | `/api/chat/scene-index` | 必需 | 否 | 索引列表，非敏感 |
| GET | `/api/chat/workflow-index` | 必需 | 否 | 索引列表，非敏感 |
| GET | `/api/chat/load-workflow` | 必需 | 是 | 所有权校验 |

### 工作流管理（Workflow Management）

| Method | Path | 鉴权 | 租户隔离 | 隔离方式 |
|--------|------|------|----------|----------|
| POST | `/api/workflows/publish` | 必需 | 是 | 发布时绑定 tenant_id |
| GET | `/api/workflows` | 必需 | 是 | 列表过滤 |
| GET | `/api/workflows/{workflow_id}` | 必需 | 是 | 所有权校验 |
| PUT | `/api/workflows/{workflow_id}` | 必需 | 是 | 所有权校验 |
| DELETE | `/api/workflows/{workflow_id}` | 必需 | 是 | 所有权校验 |
| POST | `/api/workflows/{workflow_id}/execute` | 必需 | 是 | 所有权校验 |

### 事件管理（Event Management）

| Method | Path | 鉴权 | 租户隔离 | 隔离方式 |
|--------|------|------|----------|----------|
| GET | `/api/events` | 必需 | 是 | 列表过滤 |
| POST | `/api/events` | 必需 | 是 | 创建时绑定 |

### Agent 注册（Agent Registry）

| Method | Path | 鉴权 | 租户隔离 | 隔离方式 |
|--------|------|------|----------|----------|
| GET | `/api/agents` | 必需 | 是 | 列表过滤 |
| GET | `/api/agents/{agent_id}` | 必需 | 是 | 所有权校验 |

### 工具管理（Tool Management）

| Method | Path | 鉴权 | 租户隔离 | 隔离方式 |
|--------|------|------|----------|----------|
| GET | `/api/tools` | 必需 | 否 | 全局注册表，非租户范围 |
| GET | `/api/tools/{tool_name}` | 必需 | 否 | 全局注册表 |
| DELETE | `/api/tools/{tool_name}` | 必需 | 否 | 全局注册表 |

### WebSocket 端点

| 端点 | 鉴权 | 租户隔离 | 说明 |
|------|------|----------|------|
| `GET /`（WS 升级） | 必需 | 是 | Agent 连接，tenant_id 绑定到 ConnectedServer |
| `GET /subscribe`（WS 升级） | 必需 | 是 | 事件订阅，tenant_id 用于广播过滤 |

---

## 接口详细设计

### 1. 健康检查

**`GET /api/health`**

免鉴权。用于负载均衡健康探针。

**响应**：
```
HTTP/1.1 200 OK
Content-Type: application/json

{
  "status": "ok",
  "service": "websocket_server",
  "timestamp": "2026-08-12T10:30:00Z"
}
```

---

### 2. 服务管理

#### 2.1 列出服务器

**`GET /api/servers`**

**查询参数**：无

**租户隔离**：返回 `tenant_id` 匹配的记录（兼容模式下含 NULL）

**响应**：
```json
{
  "success": true,
  "servers": [
    {
      "server_id": "srv-001",
      "name": "exec-server-1",
      "status": "online",
      "source": "execution_server",
      "connected": true,
      "total_quota": 5,
      "running_count": 2,
      "tenant_id": "c928e08e-...",
      "env_info": { "hostname": "...", "ip": "...", "os": "..." },
      "last_heartbeat": "2026-08-12T10:30:00Z"
    }
  ],
  "total": 1
}
```

#### 2.2 获取服务器详情

**`GET /api/servers/{server_id}`**

**路径参数**：`server_id` — 服务器 ID

**租户隔离**：所有权校验，不匹配返回 404

**响应**：
```json
{
  "success": true,
  "server": { "server_id": "...", "name": "...", "..." : "..." }
}
```

**错误**：
- `404` — `{"success": false, "error": "Server not found"}`

#### 2.3 清理离线服务器

**`POST /api/servers/offline`**

**请求体**：无

**租户隔离**：仅删除当前租户的离线服务器

**响应**：
```json
{
  "success": true,
  "deleted": 2
}
```

#### 2.4 删除服务器

**`DELETE /api/servers/{server_id}`**

**路径参数**：`server_id`

**租户隔离**：所有权校验。在线服务器拒绝删除。

**响应**：
```json
{
  "success": true,
  "message": "Server srv-001 deleted"
}
```

**错误**：
- `404` — `{"success": false, "error": "Server not found"}`
- `409` — `{"success": false, "error": "Server is online, cannot to delete"}`

#### 2.5 派发任务

**`POST /api/tasks/dispatch`**

**请求体**：
```json
{
  "server_id": "srv-001",
  "task_id": "task-501",
  "goal": "分析用户行为数据",
  "context": { "...": "..." }
}
```

**租户隔离**：
1. 校验 `server_id` 属于当前租户
2. 创建的 `Message` 记录绑定 `tenant_id`
3. 通过 WS 发送 TASK 帧到目标服务器

**响应**：
```json
{
  "success": true,
  "message": "Task dispatched to server srv-001"
}
```

**错误**：
- `404` — `{"success": false, "error": "Server not found or access denied"}`
- `404` — `{"success": false, "error": "Task not found"}`

---

### 3. 场景管理

#### 3.1 创建场景

**`POST /api/scenarios`**

**请求体**：
```json
{
  "name": "自动化测试流水线",
  "scenario_type": "standard",
  "config": { "key": "value" },
  "context": { "...": "..." }
}
```

**租户隔离**：创建后立即调用 `repo.update_tenant_id(scenario_id, tenant_id)` 绑定租户。广播事件携带 `tenant_id`。

**响应**：
```json
{
  "success": true,
  "scenario": {
    "scenario_id": "sc-101",
    "name": "自动化测试流水线",
    "state": "created",
    "tenant_id": "c928e08e-...",
    "created_at": "2026-08-12T10:30:00Z"
  }
}
```

#### 3.2 列出场景

**`GET /api/scenarios`**

**查询参数**：
| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `state` | string | 否 | 按状态过滤（created/running/completed/failed/stopped） |
| `limit` | int | 否 | 返回数量限制（默认 100） |

**租户隔离**：
- 有 `state` 参数：`repo.find_by_state(state, tenant_id=tenant_id)`
- 无 `state` 参数：`repo.find_all_by_tenant(tenant_id)`

**响应**：
```json
{
  "success": true,
  "scenarios": [
    {
      "scenario_id": "sc-101",
      "name": "自动化测试流水线",
      "state": "running",
      "scenario_type": "standard",
      "tenant_id": "c928e08e-...",
      "created_at": "2026-08-12T10:30:00Z"
    }
  ],
  "total": 1
}
```

#### 3.3 获取场景详情

**`GET /api/scenarios/{scenario_id}`**

**租户隔离**：所有权校验

**错误**：`404` — `{"success": false, "error": "Scenario not found"}`

#### 3.4 获取场景消息时间线

**`GET /api/scenarios/{scenario_id}/messages`**

**租户隔离**：先校验场景所有权，通过后返回该场景的所有消息

**错误**：`404` — `{"success": false, "error": "Scenario not found"}`

#### 3.5 启动场景

**`POST /api/scenarios/{scenario_id}/start`**

**请求体**：无

**租户隔离**：所有权校验。广播事件携带 `tenant_id`。

**错误**：
- `404` — `{"success": false, "error": "Scenario not found"}`
- `400` — `{"success": false, "error": "Scenario is already running"}`

#### 3.6 停止场景

**`POST /api/scenarios/{scenario_id}/stop`**

**租户隔离**：同启动

**错误**：
- `404` — `{"success": false, "error": "Scenario not found"}`

#### 3.7 删除场景

**`DELETE /api/scenarios/{scenario_id}`**

**租户隔离**：所有权校验

**错误**：`404` — `{"success": false, "error": "Scenario not found"}`

---

### 4. 任务管理

#### 4.1 列出任务

**`GET /api/tasks`**

**查询参数**：
| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `scenario_id` | string | 否 | 按场景过滤 |
| `state` | string | 否 | 按状态过滤（pending/running/success/failed/cancelled） |
| `limit` | int | 否 | 返回数量限制 |

**租户隔离**：
- 有 `scenario_id`：`repo.find_by_scenario_id(scenario_id, tenant_id=tenant_id)`
- 有 `state`：`repo.find_by_state(state, tenant_id=tenant_id)`
- 均无：`repo.find_all_by_tenant(tenant_id)`

#### 4.2 获取任务详情

**`GET /api/tasks/{task_id}`**

**租户隔离**：所有权校验

**错误**：`404` — `{"success": false, "error": "Task not found"}`

#### 4.3 取消任务

**`DELETE /api/tasks/{task_id}`**

**租户隔离**：所有权校验

**错误**：`404` — `{"success": false, "error": "Task not found"}`

#### 4.4 人工验收

**`POST /api/tasks/{task_id}/accept`**

**请求体**：
```json
{
  "approved": true,
  "feedback": "结果符合预期"
}
```

**租户隔离**：所有权校验。广播 `task_reviewed` 事件携带 `tenant_id`。

**错误**：`404` — `{"success": false, "error": "Task not found"}`

---

### 5. 聊天 / 场景助手

#### 5.1 聊天

**`POST /api/chat`**

**请求体**：
```json
{
  "message": "帮我创建一个数据分析场景",
  "session_id": "session-001"
}
```

**租户隔离**：聊天基于 session，不做租户过滤。但 LLM 生成的场景/工作流 spec 在保存时会绑定 tenant_id。

#### 5.2 保存场景

**`POST /api/chat/save`**

**请求体**：
```json
{
  "spec": { "name": "...", "config": { "..." : "..." } }
}
```

**租户隔离**：保存后调用 `ScenarioRepository().update_tenant_id(saved_id, tenant_id)`。广播 `scenario_created`/`scenario_updated` 携带 `tenant_id`。

#### 5.3 保存工作流

**`POST /api/chat/workflow/save`**

**请求体**：
```json
{
  "spec": { "name": "...", "steps": [...] },
  "workflow_id": "wf-001"
}
```

**租户隔离**：保存后设置 `wf.tenant_id = tenant_id`。广播携带 `tenant_id`。

#### 5.4 加载场景

**`GET /api/chat/load-scene?scenario_id={id}`**

**租户隔离**：所有权校验 → `404 "Scenario not found"`

#### 5.5 加载工作流

**`GET /api/chat/load-workflow?workflow_id={id}`**

**租户隔离**：所有权校验 → `404 "Workflow not found"`

#### 5.6 场景索引 / 工作流索引 / 预览

**`GET /api/chat/scene-index`**
**`GET /api/chat/workflow-index`**
**`GET /api/chat/preview`**

不做租户过滤（索引/预览功能，非敏感数据）。

---

### 6. 工作流管理

#### 6.1 发布工作流

**`POST /api/workflows/publish`**

**请求体**：
```json
{
  "scenario_id": "sc-101",
  "name": "数据分析流水线",
  "description": "从场景提取的可复用工作流"
}
```

**租户隔离**：发布后设置 `workflow.tenant_id = tenant_id`。广播 `workflow_published` 携带 `tenant_id`。

**响应**：
```json
{
  "success": true,
  "workflow": {
    "workflow_id": "wf-001",
    "name": "数据分析流水线",
    "version": 1,
    "step_count": 3,
    "input_schema": { "type": "object", "properties": { "..." : "..." } },
    "dag_definition": { "steps": [...] },
    "experience_context": [...]
  }
}
```

**错误**：
- `400` — 缺少 `scenario_id` 或场景无终态任务

#### 6.2 列出工作流

**`GET /api/workflows`**

**查询参数**：
| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `state` | string | 否 | 按状态过滤（draft/active/archived） |
| `limit` | int | 否 | 返回数量限制 |

**租户隔离**：列表过滤

#### 6.3 获取工作流详情

**`GET /api/workflows/{workflow_id}`**

**租户隔离**：所有权校验

**响应**：
```json
{
  "success": true,
  "workflow": {
    "workflow_id": "wf-001",
    "name": "数据分析流水线",
    "templates": [
      {
        "template_id": "tpl-001",
        "step_id": "step_1",
        "goal_template": "分析 {{dataset}} 的用户行为",
        "depends_on": [],
        "agent_role": "analyst",
        "server_id": "srv-001",
        "timeout_seconds": 300
      }
    ]
  }
}
```

**错误**：`404` — `{"success": false, "error": "Workflow not found"}`

#### 6.4 更新工作流

**`PUT /api/workflows/{workflow_id}`**

**请求体**：
```json
{
  "name": "新名称",
  "description": "新描述",
  "state": "active"
}
```

**租户隔离**：所有权校验

**错误**：
- `404` — `{"success": false, "error": "Workflow not found"}`
- `400` — `{"success": false, "error": "Invalid JSON"}`

#### 6.5 删除工作流

**`DELETE /api/workflows/{workflow_id}`**

**租户隔离**：所有权校验。先删除关联的 `WorkflowTaskTemplate`，再删除 `Workflow`。

**错误**：`404` — `{"success": false, "error": "Workflow not found"}`

#### 6.6 执行工作流

**`POST /api/workflows/{workflow_id}/execute`**

**请求体**：
```json
{
  "input_params": {
    "dataset": "user_behavior_2026Q3",
    "output_format": "pdf"
  }
}
```

**租户隔离**：所有权校验。广播 `workflow_execution_started` 携带 `tenant_id`。

**响应**：
```json
{
  "success": true,
  "execution": {
    "scenario_id": "sc-202",
    "workflow_id": "wf-001",
    "state": "running",
    "total_steps": 3
  }
}
```

**错误**：
- `404` — `{"success": false, "error": "Workflow not found"}`
- `400` — 输入参数校验失败

---

### 7. 事件管理

#### 7.1 列出事件

**`GET /api/events`**

**查询参数**：
| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `event_type` | string | 否 | 按事件类型过滤 |
| `trace_id` | string | 否 | 按追踪 ID 过滤 |
| `limit` | int | 否 | 返回数量限制 |

**租户隔离**：列表过滤

#### 7.2 创建事件

**`POST /api/events`**

**请求体**：
```json
{
  "event_type": "custom_event",
  "data": { "key": "value" },
  "trace_id": "trace-001"
}
```

**租户隔离**：创建时绑定 `tenant_id`。广播携带 `tenant_id`。

---

### 8. Agent 注册

#### 8.1 列出 Agent

**`GET /api/agents`**

**查询参数**：
| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `type` | string | 否 | 按 Agent 类型过滤 |
| `status` | string | 否 | 按状态过滤（内存中过滤） |
| `limit` | int | 否 | 返回数量限制 |

**租户隔离**：列表过滤

#### 8.2 获取 Agent 详情

**`GET /api/agents/{agent_id}`**

**租户隔离**：所有权校验

**错误**：`404` — `{"success": false, "error": "Agent not found"}`

---

### 9. 工具管理

工具注册表为全局内存结构，**不做租户隔离**。所有租户共享同一工具注册表。

#### 9.1 列出工具

**`GET /api/tools`**

**响应**：
```json
{
  "success": true,
  "tools": [
    { "name": "web_search", "description": "搜索互联网", "parameters": { "..." : "..." } }
  ]
}
```

#### 9.2 获取工具详情

**`GET /api/tools/{tool_name}`**

#### 9.3 注销工具

**`DELETE /api/tools/{tool_name}`**

支持热替换：注销后 Agent 下次调用时将收到工具不可用提示。

---

## WebSocket 连接鉴权

### Agent 连接（`GET /`）

**握手阶段鉴权**：

```
Client                              ws_server
  │                                     │
  │── WS Upgrade ──────────────────────→│
  │   ?api_key=<JWT>                    │
  │   (或 Authorization header)         │
  │                                     │
  │   authenticate_ws_connection()      │
  │   ├─ 验证 JWT 签名                  │
  │   ├─ 检查 exp                       │
  │   └─ 提取 tenant_id                │
  │                                     │
  │←── 101 Switching Protocols ─────────│  (成功)
  │   或 401 Unauthorized               │  (失败)
  │                                     │
  │── register { server_id, ... } ─────→│
  │                                     │ ConnectedServer.tenant_id = tenant_id
  │                                     │
  │←── TASK { task_id, goal } ─────────│
  │                                     │
  │── task_result { task_id, ... } ────→│
  │                                     │
```

**连接后行为**：
- `ConnectedServer` 记录包含 `tenant_id`
- 任务事件（`task.execution_agent_created` 等）自动携带连接的 `tenant_id`
- 心跳检测：`HEARTBEAT_INTERVAL`（默认 5s），`HEARTBEAT_TIMEOUT`（默认 15s）

### 事件订阅（`GET /subscribe`）

**握手阶段鉴权**：同 Agent 连接

**连接后行为**：
- `self.subscribers[ws] = tenant_id`
- 仅接收 `tenant_id` 匹配的事件广播
- 发送超时：2s/次，连续 3 次失败后断开

---

## 实时事件广播隔离

### 广播架构

```
event_bus.emit(event)
  │
  ▼
_broadcast_event(event_type, payload)
  │  payload 包含 tenant_id
  ▼
_broadcast_queue (asyncio.Queue, maxsize=500)
  │
  ▼
_broadcast_worker() (单消费者协程)
  │
  ├─ event_tenant = payload.get("tenant_id")
  │
  └─ for (ws, subscriber_tenant) in subscribers:
       │
       ├─ event_tenant 存在 且 subscriber_tenant != event_tenant
       │   └─ SKIP（租户隔离）
       │
       ├─ event_tenant 为 None
       │   └─ 广播给所有订阅者（向后兼容）
       │
       └─ event_tenant == subscriber_tenant
           └─ ws.send_json(payload)
```

### 事件类型与租户传播

| 事件模式 | 示例事件 | tenant_id 来源 |
|----------|----------|----------------|
| `scenario.*` | `scenario_created`, `scenario_state_changed` | 场景记录 |
| `task.*` | `task_state_changed`, `task_execution_started` | 任务记录 / ConnectedServer |
| `topic.*` | `topic_progress` | 关联场景 |
| `workflow.*` | `workflow_published`, `workflow_execution_started` | 工作流记录 |
| `recovery.*` | `recovery_heartbeat` | 系统事件 |

### 广播参数

| 参数 | 值 | 说明 |
|------|-----|------|
| 队列容量 | 500 | 超出后新事件丢弃（non-blocking put） |
| 发送超时 | 2s | 单次 `send_json` 超时 |
| 失败阈值 | 3 | 连续失败次数达到后移除订阅者 |

---

## 错误响应规范

### 鉴权错误（HTTP 401）

| error 值 | 触发条件 |
|----------|----------|
| `"Unauthorized: Missing API key"` | 请求未携带任何 Token |
| `"Unauthorized: Token expired"` | JWT `exp` 已过期 |
| `"Unauthorized: Invalid token"` | JWT 签名不匹配或格式错误 |
| `"Unauthorized: Missing tenantId in token payload"` | JWT payload 缺少 `tenantId` 字段 |

### 资源错误（HTTP 404）

统一使用 404（而非 403）防止资源存在性枚举：

| error 值 | 触发条件 |
|----------|----------|
| `"Scenario not found"` | 场景不存在或不属于当前租户 |
| `"Task not found"` | 任务不存在或不属于当前租户 |
| `"Server not found"` | 服务器不存在或不属于当前租户 |
| `"Server not found or access denied"` | 任务派发时服务器不属于当前租户 |
| `"Agent not found"` | Agent 不存在或不属于当前租户 |
| `"Workflow not found"` | 工作流不存在或不属于当前租户 |

### 业务错误（HTTP 400）

| error 值 | 触发条件 |
|----------|----------|
| `"Scenario is already running"` | 重复启动运行中的场景 |
| `"Missing scenario_id"` | 发布工作流时缺少场景 ID |
| 输入验证详情 | 工作流执行参数校验失败 |

### 冲突错误（HTTP 409）

| error 值 | 触发条件 |
|----------|----------|
| `"Server is online, cannot to delete"` | 尝试删除在线服务器 |

### 标准错误响应格式

```json
{
  "success": false,
  "error": "错误描述信息"
}
```

---

## 配置参考

### 鉴权相关

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `WS_JWT_SECRET` | `ws-platform-jwt-secret-2026` | HMAC-SHA256 签名密钥。必须与平台前端生成 API Key 时使用的密钥一致 |
| `WS_TENANT_STRICT` | `false` | 租户隔离模式。`false` = 兼容模式（NULL tenant_id 可见），`true` = 严格模式 |

### 传输安全

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `WS_SSL_CERT` | 无 | TLS 证书路径。设置后启用 WSS+HTTPS 单端口模式 |
| `WS_SSL_KEY` | 无 | TLS 私钥路径 |

### 连接参数

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `WS_HOST` | `0.0.0.0` | 监听地址 |
| `WS_PORT` | `8765` | 监听端口 |
| `HEARTBEAT_INTERVAL` | `5` | Agent 心跳间隔（秒） |
| `HEARTBEAT_TIMEOUT` | `15` | Agent 心跳超时（秒），超时后断开 |

---

## 数据迁移

### 添加 tenant_id 列

```python
from database.migration_tenant import migrate_add_tenant_id

migrate_add_tenant_id()
```

对 12 张表执行：
1. `ALTER TABLE {table} ADD COLUMN tenant_id TEXT`（已存在则跳过）
2. `CREATE INDEX IF NOT EXISTS idx_{table}_tenant ON {table}(tenant_id)`

### 回填默认租户

```python
from database.migration_tenant import migrate_set_default_tenant

migrate_set_default_tenant("default-tenant-uuid")
```

对所有 12 张表执行：
```sql
UPDATE {table} SET tenant_id = ? WHERE tenant_id IS NULL
```

### 迁移后切换严格模式

回填完成后，设置 `WS_TENANT_STRICT=true` 启用严格隔离。

---

## 附录：代码模块索引

| 模块 | 路径 | 职责 |
|------|------|------|
| JWT 工具 | `auth/jwt_utils.py` | HS256 签名验证、Token 提取、异常类定义 |
| REST 中间件 | `auth/middleware.py` | aiohttp 中间件、白名单、tenant_id 注入 |
| WS 鉴权 | `auth/ws_auth.py` | WebSocket 握手鉴权 |
| 配置 | `websocket_server/config.py` | 环境变量读取 |
| 服务器 | `websocket_server/server.py` | WS Hub、广播、连接管理 |
| API 路由 | `websocket_server/api.py` | 服务管理路由 + 路由注册 |
| 场景路由 | `websocket_server/api_routes_scenario.py` | 场景 CRUD |
| 任务路由 | `websocket_server/api_routes_task.py` | 任务管理 |
| 聊天路由 | `websocket_server/api_routes_chat.py` | 场景助手 |
| 事件路由 | `websocket_server/api_routes_event.py` | 事件管理 |
| Agent 路由 | `websocket_server/api_routes_agent.py` | Agent 注册 |
| 工具路由 | `websocket_server/api_routes_tool.py` | 工具管理 |
| 工作流路由 | `websocket_server/api_routes_workflow.py` | 工作流管理 |
| 基础仓库 | `database/repositories/base_repository.py` | `_tenant_query()` 通用租户查询 |
| 迁移脚本 | `database/migration_tenant.py` | tenant_id 列添加与数据回填 |

---

**文档版本**: 2.0
**最后更新**: 2026-08-12
**维护者**: LLM Harness Team
