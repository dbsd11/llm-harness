# LLM Harness

A unified, distributed agent-server platform for multi-agent orchestration.
Three peers collaborate over WebSocket/HTTP:

- a **websocket_server** (core backend: REST API + WS hub + scheduling/scenario engine + workflow DAG engine + scene assistant + JWT auth + multi-tenant isolation + event broadcast),
- an **agent_server_platform** (presentation layer: Gradio UI + event sync + human-agent relay), and
- one or more **execution_agent_server** (sandboxed LLM task runners).

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  websocket_server (core backend, aiohttp :8765)                     │
│                                                                      │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌───────────┐  │
│  │Scheduling    │ │Scenario      │ │SceneAssistant│ │REST API   │  │
│  │Agent         │ │Manager       │ │(chatbot +    │ │33 endpoints│  │
│  │decompose+    │ │create+start+ │ │workflow mgmt)│ │scenarios,  │  │
│  │dispatch      │ │stop+review   │ │              │ │tasks,agents│  │
│  └──────┬───────┘ └──────┬───────┘ └──────┬───────┘ │workflows…  │  │
│         │                │                │         └─────┬───────┘  │
│  ┌──────────────┐ ┌──────┴───────────────────────────────┴────────┐ │
│  │Workflow      │ │              CentralDispatcher                │ │
│  │Publisher +   │ │  polls dispatch rows (acked=0) → routes to    │ │
│  │Executor      │ │  WS or local exec                             │ │
│  │(DAG engine)  │ └──────────────────────────┬────────────────────┘ │
│  └──────────────┘                             │                      │
│  ┌──────────────────────────┬─────────────────────────────────────┐ │
│  │  Auth Middleware (JWT)   │  WS Hub + Event Broadcast           │ │
│  │  HS256 verify → tenant   │  / (agent connections)              │ │
│  │  injection on every req  │  /subscribe (tenant-scoped stream)  │ │
│  └──────────────────────────┘  register/heartbeat/task/ack        │ │
│                                                                      │
│  Owns all 12 DB tables: scenarios, tasks, events, messages,          │
│  agents, users, execution_servers, human_tasks, assistant_messages,  │
│  consumer_offsets, workflows, workflow_task_templates                │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                 REST (8765) + WS /subscribe
                           │
┌──────────────────────────┴──────────────────────────────────────────┐
│  agent_server_platform (presentation layer, Gradio :8080)            │
│                                                                      │
│  ┌────────────────────┐  ┌──────────────────┐  ┌────────────────┐   │
│  │ Gradio UI          │  │ HumanAgentClient │  │WSEventSubscriber│   │
│  │ Dashboard          │  │ (WS / agent)     │  │ (WS /subscribe) │   │
│  │ Scenario Dashboard │  │ register/heart-  │  │ All events →    │   │
│  │ Workflow DAG       │  │ beat/recv TASK   │  │ local events    │   │
│  │ Task Monitor       │  │ send task_result │  │ table           │   │
│  │ Agent Registry     │  │                  │  │                 │   │
│  │ Execution Servers  │  │                  │  │                 │   │
│  │ Human Tasks        │  │                  │  │                 │   │
│  │ Event Log          │  └────────┬─────────┘  └────────┬────────┘   │
│  │                    │           │                      │            │
│  │ All writes via     │           │                      │            │
│  │ api_client →       │           │                      │            │
│  │ ws_server REST     │           │                      │            │
│  └────────────────────┘           │                      │            │
│                                    │                      │            │
│  Local DB: events, messages,       │                      │            │
│  human_tasks tables                │                      │            │
└────────────────────────────────────┼──────────────────────┼──────────┘
                                     │                      │
                           WS / (agent protocol)           │
                                     │                      │
┌────────────────────────────────────┼──────────────────────┼──────────┐
│  execution_agent_server (Docker, ≥1 instance)             │          │
│                                                            │          │
│  WS / — register/heartbeat/recv TASK/send task_result     │          │
│  ThreadPool — run ExecutionAgent (LLM) for each task      │          │
└────────────────────────────────────────────────────────────┴──────────┘
```

## Task Flow (End-to-End)

### Execution Agent (LLM)

```
User → Gradio UI → api_client.create_scenario() → POST /api/scenarios → ws_server
  → scenario_manager.create_scenario() → DB write

User → Gradio UI → api_client.start_scenario() → POST /api/scenarios/{id}/start → ws_server
  → scenario_manager.start_scenario() → spawns scenario thread
  → agent_manager.submit_task(goal, type="scheduling")
  → SchedulingAgent.decompose(goal) → subtasks with DAG
  → mqs.dispatch_subtasks() → writes dispatch Message rows (acked=0)
  → CentralDispatcher polls → routes to execution-agent-server via WS TASK frame
  → exec-server runs ExecutionAgent (LLM) → sends task_result frame
  → ws_server finalize_task() → writes reply + broadcasts via /subscribe
  → SchedulingAgent.collect_replies() → advances DAG → next wave

Platform UI refreshes by polling api_client (scenario/task state from ws_server REST).
Event Log shows real-time events synced by WSEventSubscriber.
```

### Human Agent (Human-in-the-Loop)

```
SchedulingAgent → associates execution role with server_id=human-1
  → CentralDispatcher forwards TASK frame via WS to HumanAgentClient
  → HumanAgentClient receives → sends ACK → persists to local human_tasks table
  → Gradio /human_tasks page polls DB every 5s → shows task tabs
  → Human types answer → clicks submit
  → HumanAgentClient.send_result() → task_result frame via WS
  → ws_server finalize_task() → broadcast via /subscribe
  → SchedulingAgent collects reply → advances DAG
```

### Scene Assistant Chatbot

```
User → Gradio chatbot floating window
  → api_client.chat(message) → POST /api/chat → ws_server
  → SceneAssistant (LLM-driven multi-turn dialogue)
  → generates scene-spec draft → user confirms
  → api_client.chat_save(spec) → POST /api/chat/save → ws_server
  → scenario_manager.create_scenario() → DB write

Also supports workflow management via natural language:
  → gather workflow index / summarize existing workflows
  → parse workflow-spec code blocks → validate DAG → render preview
  → save/load workflows via chat conversation
```

### Workflow System (DAG Pipelines)

```
Publish: completed/failed scenario → POST /api/workflows/publish
  → WorkflowPublisher: extract DAG via topological sort
  → extract parameters (config values in goals → {{variable}} placeholders)
  → collect experience context (per-step state/result/error)
  → generate JSON Schema for inputs
  → persist Workflow + WorkflowTaskTemplate records

Execute: POST /api/workflows/{id}/execute with input_params
  → WorkflowExecutor: validate inputs against schema
  → create execution scenario (type=workflow_execution)
  → materialize tasks from templates (render {{param}} → actual values)
  → build execution waves via topological sort
  → dispatch wave-by-wave (same wave = parallel, cross-wave = serial)
  → inject upstream results into downstream context
  → propagate failures to dependent steps
```

## Key Properties

- **Single source of truth**: ws_server owns all business data. platform has only local events/messages/human_tasks tables synced via `/subscribe`.
- **Presentation-only platform**: all Gradio pages query ws_server REST API or local tables. No business logic in platform.
- **Ack-on-receipt**: agents ACK immediately on receiving a TASK frame. Crash before ACK → re-send on next heartbeat.
- **Same protocol for all agents**: execution-agent servers and human-agent clients use identical WS frames. Routing is by `server_id`.
- **Event-driven sync**: WSEventSubscriber listens on `/subscribe`, stores all events in local events/messages/human_tasks tables. Dashboard, Event Log, and Flask API read from these tables.
- **Multi-tenant isolation**: all data (12 tables + WS broadcasts) scoped by `tenant_id`, enforced at REST, WS, and DB layers.
- **Workflow reuse**: completed scenarios can be published as parameterized DAG workflows and re-executed with new inputs.

## API Key Authentication

ws_server uses JWT (HS256) API Keys for authentication. The platform frontend generates API Keys; ws_server independently verifies them — no shared session state.

### API Key Format

API Keys are standard 3-segment JWT tokens (`header.payload.signature`). Payload structure:

```json
{
  "tenantId": "c928e08e-1234-5678-9abc-def012345678",
  "iat": 1770610935,
  "exp": 1802146935
}
```

| Field | Type | Description |
|---|---|---|
| `tenantId` | string (UUID) | Tenant identifier |
| `iat` | number | Issued-at timestamp (seconds) |
| `exp` | number | Expiry timestamp (seconds, default 1 year) |

### Verification Flow

```
Request (HTTP / WS)
  → Extract API Key
  → Split 3-segment JWT
  → HMAC-SHA256 signature verify (constant-time comparison)
  → Check exp (reject if expired)
  → Parse tenantId from payload
  → Inject into request context
```

Zero-dependency implementation in `auth/jwt_utils.py` — no PyJWT or cryptography libraries needed.

### REST API Authentication

All `/api/*` routes (except `/api/health`) require a valid API Key, enforced by `auth/middleware.py`:

| Header | Format |
|---|---|
| `Authorization` | `Bearer <JWT>` |
| `X-API-Key` | `<JWT>` |

On success, `tenant_id` is injected into `request["tenant_id"]` for downstream use. On failure, returns `401 Unauthorized`.

### WebSocket Authentication

Both WS endpoints (`/` for agent connections, `/subscribe` for event streams) require authentication at handshake, handled by `auth/ws_auth.py`:

| Method | Format |
|---|---|
| URL query param | `?api_key=<JWT>` |
| HTTP header | `Authorization: Bearer <JWT>` |
| WS subprotocol | `Sec-WebSocket-Protocol: api-key.<JWT>` |

On failure, the connection is rejected with HTTP 401.

### Configuration

| Var | Default | Description |
|---|---|---|
| `WS_JWT_SECRET` | `ws-platform-jwt-secret-2026` | HMAC-SHA256 signing key. Must match the key used by the platform to generate API Keys. |

## Multi-Tenant Isolation

Every resource in ws_server is scoped to a tenant. The `tenant_id` (extracted from the JWT API Key) flows through every layer:

### Data Model

All 12 database tables include a `tenant_id` column with indexes:

| Table | Description |
|---|---|
| `users` | User accounts |
| `agents` | Registered agent instances |
| `scenarios` | Scenario definitions |
| `tasks` | Execution tasks (DAG nodes) |
| `events` | System event log |
| `messages` | Inter-agent dispatch/reply messages |
| `consumer_offsets` | Event consumer positions |
| `execution_servers` | Connected execution servers |
| `human_tasks` | Human-in-the-loop task queue |
| `assistant_messages` | Scene assistant chat history |
| `workflows` | Reusable workflow definitions |
| `workflow_task_templates` | Workflow step templates |

### Isolation at Each Layer

**REST API** — `auth/middleware.py` injects `tenant_id`; every route filters queries and checks ownership:
- List endpoints (`GET /api/scenarios`, `/api/tasks`, etc.) return only rows matching `tenant_id`
- Mutations (`POST /api/tasks/dispatch`, etc.) validate the target resource belongs to the calling tenant

**WebSocket broadcast** — Each connection is tagged with `tenant_id` at handshake. The broadcast worker only pushes events to subscribers with matching `tenant_id`:
```python
async def broadcast_event(event):
    for conn in active_connections:
        if conn.tenant_id == event.tenant_id and conn.is_open():
            await conn.send_json(event)
```

**Server registration** — `ConnectedServer` stores `tenant_id`; upsert and dispatch validate server ownership per tenant.

### Migration

`database/migration_tenant.py` provides:
- `migrate_add_tenant_id()` — ALTER TABLE + index creation for all 12 tables
- `migrate_set_default_tenant(default_tenant_id)` — one-time backfill of NULL rows

### Strictness Modes

| Var | Value | Behavior |
|---|---|---|
| `WS_TENANT_STRICT` | `false` (default) | Backward-compatible: NULL `tenant_id` rows accessible by all tenants |
| `WS_TENANT_STRICT` | `true` | Strict isolation: only rows with matching `tenant_id` are visible |

## Repository Layout

```
llm-harness/
├── src/
│   ├── websocket_server/          # core backend: aiohttp WS+REST hub + all business logic
│   │   ├── auth/                  # JWT API Key verification, REST middleware, WS handshake auth
│   │   ├── core/                  # CentralDispatcher, MessageQueue, SandboxManager
│   │   ├── database/              # models, repositories, migration_tenant
│   │   ├── services/              # SchedulingAgent, ScenarioManager, SceneAssistant, WorkflowPublisher/Executor
│   │   └── websocket_server/      # API routes (scenario, task, agent, event, chat, workflow, server, tool)
│   ├── agent_server_platform/     # presentation layer: Gradio UI + event sync + human-agent relay
│   └── execution_agent_server/    # sandboxed LLM task runner (Docker)
├── scripts/
│   ├── build/                     # Dockerfiles per component
│   └── deploy/                    # deploy.sh per component + TLS cert management
├── docs/
│   ├── WEBSOCKET_SERVER_API_KEY_AUTH_DESIGN.md
│   └── workflow-system-design.md
├── README.md
└── .gitignore
```

## Components

| Component | Path | Role | Port |
|---|---|---|---|
| **websocket_server** | `src/websocket_server/` | **Core backend**: 12 DB tables, JWT auth, multi-tenant isolation, SchedulingAgent, ScenarioManager, SceneAssistant, WorkflowPublisher/Executor, CentralDispatcher, REST API (33 endpoints), WS hub, event broadcast | 8765 (WS + REST) |
| **agent_server_platform** | `src/agent_server_platform/` | **Presentation layer**: Gradio UI (Dashboard, Scenario Dashboard, Workflow DAG, Task Monitor, Agent Registry, Execution Servers, Human Tasks, Event Log), HumanAgentClient, WSEventSubscriber, api_client | 8080 (Gradio) |
| **execution_agent_server** | `src/execution_agent_server/` | Sandboxed LLM task runner; connects to WS server, executes TASK frames | - (outbound WS) |

## Quick Start

```bash
# 1. WebSocket server (core backend) — listens on :8765 (WS + REST)
cd src/websocket_server
cp .env.example .env
pip install -r requirements.txt
python -m websocket_server

# 2. Execution-agent server — connects to WS server, runs LLM tasks
cd src/execution_agent_server
cp .env.example .env
pip install -r requirements.txt
python -m execution_server

# 3. Agent server platform (presentation) — Gradio UI
cd src/agent_server_platform
cp .env.example .env
python src/app.py
```

## Deployment (Docker)

```bash
# WS server (core backend)
bash scripts/deploy/websocket_server.sh

# Execution-agent servers
bash scripts/deploy/execution_agent_server.sh

# Agent server platform (presentation)
bash scripts/deploy/agent_server_platform.sh
```

## Configuration

| Var | Set in | Meaning |
|---|---|---|
| `WS_JWT_SECRET` | ws_server `.env` | JWT signing key for API Key verification (must match platform) |
| `WS_TENANT_STRICT` | ws_server `.env` | `true` = strict tenant isolation, `false` = backward-compatible (default) |
| `WS_SSL_CERT` / `WS_SSL_KEY` | ws_server `.env` | TLS certificate/key paths (enables WSS+HTTPS on single port) |
| `WS_SERVER_API_URL` | platform `.env` | HTTP base for ws_server REST API (e.g. `https://host:8765`) |
| `WS_SERVER_WS_URL` | platform `.env` | WS URL for HumanAgentClient and WSEventSubscriber (e.g. `wss://host:8765`) |
| `DASHSCOPE_API_KEY` | platform + ws_server `.env` | LLM API key (both need it: platform for compression, ws_server for chat/scheduling) |
| `LLM_BASE_URL` | platform + ws_server `.env` | LLM API base URL |
| `LLM_MODEL` | platform + ws_server `.env` | LLM model name (e.g. `qwen3.7-plus`) |
| `SANDBOX_SSH_HOST` | platform `.env` | SSH host for Docker sandbox management |
| `SANDBOX_DOCKER_IMAGE` | platform `.env` | Docker image for sandbox containers |

## ws_server REST API

All endpoints except `/api/health` require JWT API Key authentication (see [API Key Authentication](#api-key-authentication)).

### Server Management

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check (no auth) |
| GET | `/api/servers` | List servers (tenant-filtered) |
| GET | `/api/servers/{server_id}` | Get server detail (tenant-checked) |
| POST | `/api/servers/offline` | Cleanup offline servers |
| DELETE | `/api/servers/{server_id}` | Delete server |
| POST | `/api/tasks/dispatch` | Dispatch task to server |

### Scenario Management

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/scenarios` | Create scenario |
| GET | `/api/scenarios` | List scenarios (state/tenant filter) |
| GET | `/api/scenarios/{scenario_id}` | Get scenario |
| GET | `/api/scenarios/{scenario_id}/messages` | Message timeline |
| POST | `/api/scenarios/{scenario_id}/start` | Start scenario |
| POST | `/api/scenarios/{scenario_id}/stop` | Stop scenario |
| DELETE | `/api/scenarios/{scenario_id}` | Delete scenario |

### Task Management

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/tasks` | List tasks (scenario_id/state/tenant filter) |
| GET | `/api/tasks/{task_id}` | Get task |
| DELETE | `/api/tasks/{task_id}` | Cancel task |
| POST | `/api/tasks/{task_id}/accept` | Human acceptance review |

### Chat / Scene Assistant

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/chat` | Chat turn (scene + workflow management) |
| POST | `/api/chat/save` | Save scene spec |
| POST | `/api/chat/workflow/save` | Save workflow spec |
| GET | `/api/chat/preview` | Scene preview |
| GET | `/api/chat/load-scene` | Load scene for editing |
| GET | `/api/chat/scene-index` | Scene list for assistant |
| GET | `/api/chat/workflow-index` | Workflow list for assistant |
| GET | `/api/chat/load-workflow` | Load workflow for editing |

### Workflows

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/workflows/publish` | Publish scenario as workflow |
| GET | `/api/workflows` | List workflows (state/tenant filter) |
| GET | `/api/workflows/{workflow_id}` | Get workflow with templates |
| PUT | `/api/workflows/{workflow_id}` | Update workflow (name/description/state) |
| DELETE | `/api/workflows/{workflow_id}` | Delete workflow + templates |
| POST | `/api/workflows/{workflow_id}/execute` | Execute workflow with input params |

### Events / Agents / Tools

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/events` | List events (type/trace/tenant filter) |
| POST | `/api/events` | Create event |
| GET | `/api/agents` | List agents (type/tenant filter) |
| GET | `/api/agents/{agent_id}` | Get agent |
| GET | `/api/tools` | List registered tools |
| GET | `/api/tools/{tool_name}` | Get tool details |
| DELETE | `/api/tools/{tool_name}` | Unregister tool (hot-swap) |

## License

Internal project.