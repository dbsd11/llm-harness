# WebSocket Server API Key 鉴权与租户隔离设计规范文档

## 1. 概述与背景

当前平台前端与边缘函数（`ws-proxy`）已内置 API Key 的生成与管理机制。API Key 在本质上是一个由平台签名的 HMAC-SHA256 格式 JWT（JSON Web Token）。

为了提升系统的安全性、支持多租户独立隔离与操作权限控制，WebSocket Server（以下简称 `ws_server`）需要独立完成 API Key 的合法性校验、租户身份解析（`tenant_id`），并在内存数据结构、REST API 与 WebSocket 订阅广播中实现严格的租户隔离。

---

## 2. API Key 格式与密钥规范

### 2.1 当前 JWT 密钥 (JWT Secret)
* **JWT Secret**: `ws-platform-jwt-secret-2026`
* **签名算法**: `HS256` (HMAC-SHA256)
* **配置规范**: 在生产环境中，`ws_server` 应将该密钥从环境变量 `WS_JWT_SECRET` 中读取，若未设置则回退至默认值 `ws-platform-jwt-secret-2026`。

### 2.2 API Key Payload 结构
生成的 API Key 格式为标准的 3 段式 JWT 字符串：`headerB64.payloadB64.signature`。
解码 `payloadB64` 后的 JSON 结构如下：

```json
{
  "tenantId": "c928e08e-1234-5678-9abc-def012345678",
  "iat": 1770610935,
  "exp": 1802146935
}
```

| 字段名 | 类型 | 说明 |
| :--- | :--- | :--- |
| `tenantId` | string (UUID) | 租户唯一标识（即 Supabase Auth 中的 `user_id`） |
| `iat` | number | 签发时间戳（秒） |
| `exp` | number | 过期时间戳（秒，默认签发后 1 年） |

---

## 3. 接口鉴权与租户解析机制

### 3.1 鉴权入口设计

#### 1. REST API 鉴权
客户端调用 REST 接口时，通过 HTTP Header 传递 API Key：
```http
Authorization: Bearer <API_KEY_JWT>
```
或支持备用 Header：`X-API-Key: <API_KEY_JWT>`。

#### 2. WebSocket 长连接鉴权
建立 WSS 长连接握手（如 `/subscribe` 或 `/ws`）时，支持以下两种方式之一：
* **URL Query 参数**：`wss://agent-socket-server.bdzz.com.cn:8765/subscribe?api_key=<API_KEY_JWT>`
* **HTTP Header (握手阶段)**：`Authorization: Bearer <API_KEY_JWT>` 或 `Sec-WebSocket-Protocol: api-key, <API_KEY_JWT>`

---

### 3.2 `ws_server` 内部 JWT 校验与解析流程

```
┌────────────────────────┐
│ 收到请求 (HTTP / WSS)   │
└───────────┬────────────┘
            │
            ▼
┌────────────────────────┐
│  提取 API Key 字符串   │
└───────────┬────────────┘
            │
            ▼
┌────────────────────────┐
│  格式切分 3 段 JWT     │ ── 失败 ──► 返回 401 Unauthorized / 关闭 WS (Code 1008)
└───────────┬────────────┘
            │
            ▼
┌────────────────────────┐
│  HMAC-SHA256 签名校验  │ ── 不匹配 ──► 返回 401 Invalid Signature
└───────────┬────────────┘
            │
            ▼
┌────────────────────────┐
│ 过期时间 (exp) 校验     │ ── 已过期 ──► 返回 401 Key Expired
└───────────┬────────────┘
            │
            ▼
┌────────────────────────┐
│  解析出 tenantId       │
└───────────┬────────────┘
            │
            ▼
┌────────────────────────┐
│ 注入上下文 (Req Context)│
└────────────────────────┘
```

#### Python 内部解析示例实现代码 (ws_server 内部)

```python
import hmac
import hashlib
import base64
import json
import time

JWT_SECRET = "ws-platform-jwt-secret-2026"

def base64url_decode(input_str: str) -> bytes:
    rem = len(input_str) % 4
    if rem > 0:
        input_str += '=' * (4 - rem)
    input_str = input_str.replace('-', '+').replace('_', '/')
    return base64.b64decode(input_str)

def verify_and_parse_api_key(api_key: str, secret: str = JWT_SECRET) -> dict:
    parts = api_key.split('.')
    if len(parts) != 3:
        raise ValueError("Invalid token format")
    
    header_b64, payload_b64, sig_b64 = parts
    sig_input = f"{header_b64}.{payload_b64}".encode('utf-8')
    
    # 计算签名
    expected_sig = hmac.new(secret.encode('utf-8'), sig_input, hashlib.sha256).digest()
    expected_sig_b64 = base64.b64encode(expected_sig).decode('utf-8').replace('+', '-').replace('/', '_').rstrip('=')
    
    # 恒定时间比较防时序攻击
    if not hmac.compare_digest(sig_b64, expected_sig_b64):
        raise ValueError("Invalid signature")
    
    # 解码 Payload
    payload_json = base64url_decode(payload_b64).decode('utf-8')
    payload = json.loads(payload_json)
    
    # 校验 exp
    now = int(time.time())
    if payload.get("exp") and now > payload["exp"]:
        raise ValueError("Token expired")
        
    if "tenantId" not in payload:
        raise ValueError("Missing tenantId in payload")
        
    return payload
```

---

## 4. 租户隔离机制设计 (Tenant Isolation)

### 4.1 数据模型改造

WebSocket Server 在内存中维护的所有实体（Servers, Scenarios, Tasks, Agents, Events, Workflows）均增加 `tenant_id` 属性：

```json
// 执行服务器 (Execution Server)
{
  "server_id": "srv-001",
  "tenant_id": "c928e08e-1234-5678-9abc-def012345678",
  "status": "online",
  "host": "192.168.1.10"
}

// 场景 (Scenario)
{
  "scenario_id": "sc-101",
  "tenant_id": "c928e08e-1234-5678-9abc-def012345678",
  "name": "自动化测试流水线",
  "state": "running"
}

// 任务 (Task)
{
  "task_id": "task-501",
  "tenant_id": "c928e08e-1234-5678-9abc-def012345678",
  "scenario_id": "sc-101",
  "server_id": "srv-001",
  "state": "completed"
}

// 事件 (WsEvent)
{
  "event_id": "evt-901",
  "tenant_id": "c928e08e-1234-5678-9abc-def012345678",
  "event_type": "task_status_changed",
  "timestamp": 1770611000
}
```

---

### 4.2 REST API 过滤与权限校验

所有 REST 接口在读取与操作数据时，必须进行 `tenant_id` 约束：

#### 1. 查询列表接口 (Query Isolation)
* `GET /api/servers`: 仅返回 `server.tenant_id == current_tenant_id` 的服务器列表。
* `GET /api/scenarios`: 仅返回 `scenario.tenant_id == current_tenant_id` 的场景。
* `GET /api/tasks`: 仅返回 `task.tenant_id == current_tenant_id` 的任务。
* `GET /api/agents`: 仅返回 `agent.tenant_id == current_tenant_id` 的智能体注册记录。
* `GET /api/events`: 仅返回 `event.tenant_id == current_tenant_id` 的事件。

#### 2. 写操作与指令控制接口 (Command Authorization)
在执行修改操作时，必须校验目标资源的所有权：
```python
def handle_dispatch_task(req_context, task_data):
    current_tenant = req_context['tenant_id']
    target_server_id = task_data['server_id']
    
    server = memory_db.get_server(target_server_id)
    if not server or server.tenant_id != current_tenant:
        return {"success": False, "error": "Server not found or access denied"}, 404
        
    # 执行派发逻辑...
```

---

### 4.3 WebSocket 广播与事件推拉隔离 (Realtime Isolation)

在 WebSocket 实时事件订阅连接中：
1. **连接绑定**：握手通过后，将当前的 `WebSocketConnection` 实例附加 `connection.tenant_id = parsed_tenant_id`。
2. **选择性广播 (Targeted Broadcast)**：当系统产生新事件（如任务完成、执行服务器心跳、场景状态改变）时，只向 `conn.tenant_id == event.tenant_id` 的活跃客户端推送消息：

```python
async def broadcast_event(event: dict):
    event_tenant = event.get("tenant_id")
    for conn in active_connections:
        if conn.tenant_id == event_tenant and conn.is_open():
            await conn.send_json(event)
```

---

## 5. 数据安全保障与高级防护机制

### 5.1 API Key 撤销与实时黑名单 (Revocation & Revoke Checks)
虽然 JWT 是无状态校验，但当用户在前端删除/禁用 API Key 时，需保障被禁用的 API Key 迅速失效。
* **机制**：`ws_server` 可维护一个轻量级的撤销缓存（内存 LRU 或 Redis）或在握手/长连接期间，每隔 N 分钟对活动的 API Key 在数据库中的 `is_active` 状态进行校验。
* 如果判定 API Key 已被置为 `is_active = false`，立即中断当前 WebSocket 连接并拒绝后续 REST 请求。

### 5.2 租户速率限制 (Rate Limiting per Tenant)
为防止单租户高频发送指令打爆系统，基于 `tenant_id` 实现令牌桶/漏桶算法限流：
* **REST API**：单租户限制 100 req/s。
* **WebSocket 消息**：单租户限制 50 msgs/s。
* 超出限制返回 HTTP 429 Too Many Requests。

### 5.3 传输层安全与防重放
* **加密通信**：全部接口强制采用 TLS 1.2+ 加密（WSS / HTTPS）。
* **时间戳防重放**：对含有签名指令的请求，检查请求时间与服务器当前时间相差不大于 300 秒。

### 5.4 日志脱敏与安全审计
* 控制台及文件日志中**严禁明文打印完整的 API Key**。
* 日志中仅打印 API Key 的 Prefix 前缀（如 `eyJhbGciOiJIUzI1...`）及对应解析出的 `tenant_id`。

---

## 6. 前端与 ws-proxy 适配指南

1. **`ws-proxy` 代理透明透传**：`ws-proxy` Edge Function 在接收到前端请求后，自动查询用户生效的 `jwt_token`，并在转发至 `ws_server` 时附带在 `Authorization: Bearer <jwt_token>` 或 URL 参数中。
2. **客户端错误处理**：前端当收到 `401 Unauthorized` 或 WebSocket Code `1008` 时，弹出“API Key 无效或已过期，请重新配置”提示，并引导用户前往 `/api-keys` 页面管理。
