# LLM Harness

A unified, distributed agent-server platform for multi-agent orchestration.
Three peers collaborate over WebSocket/HTTP:

- a **websocket_server** (core backend: REST API + WS hub + scheduling/scenario engine + workflow DAG engine + scene assistant + JWT auth + multi-tenant isolation + event broadcast),
- an **agent_server_platform** (presentation layer: Gradio UI + Flask internal API + event sync + human-agent relay), and
- one or more **execution_agent_server** (sandboxed LLM task runners).

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  websocket_server (core backend, aiohttp :8765)                     │
│                                                                      │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌───────────┐  │
│  │Scheduling    │ │Scenario      │ │SceneAssistant│ │REST API   │  │
│  │Agent         │ │Manager       │ │(chatbot +    │ │38 endpoints│  │
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
│  agent_server_platform (presentation layer, Gradio :8080 + Flask :5000)│
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
│  ┌──────────────────┐  Local DB: 8 tenant-scoped tables   │            │
│  │Flask Internal API│  (events, messages, human_tasks,    │            │
│  │ :5000 (Swagger)  │   agents, scenarios, tasks,         │            │
│  │                  │   assistant_messages, users)        │            │
│  │ :5000 (Swagger)  │                                    │            │
│  └──────────────────┘                                    │            │
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

The **ws_server** side uses direct context injection (server lists, scene/workflow indexes embedded in system prompt).
The **platform** side uses a ReAct tool-calling loop — the LLM calls tools to fetch live data instead of relying on stale prompt injection.

```
User → Gradio chatbot floating window (platform)
  → SceneAssistant._react_loop(messages, tools)
  → LLM calls tools before answering:
    - list_execution_servers → query local DB (synced via /subscribe)
    - list_human_servers     → query local DB
    - list_scenarios         → query local DB
  → LLM generates scene-spec draft → user confirms
  → api_client.chat_save(spec) → POST /api/chat/save → ws_server
  → scenario_manager.create_scenario() → DB write

ws_server chat endpoint (POST /api/chat):
  → system prompt injects tenant-scoped context:
    - gather_scene_index(tenant_id)
    - gather_workflow_index(tenant_id)
    - gather_execution_servers(tenant_id)
    - gather_human_servers(tenant_id)
  → _maybe_focus / _maybe_workflow_focus inject specific scenario/workflow details
  → also supports workflow management via natural language:
    - parse workflow-spec code blocks → validate DAG → render preview
    - save/load workflows via chat conversation
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

### Assistant Mode (Plan-First Execution)

When assistant roles are configured, the system automatically enables **assistant mode** — a plan-first execution flow where plans are reviewed before execution.

```
SchedulingAgent detects assistant roles (semantic LLM analysis)
  → injects assistant_mode=True + task_type="generate_plan" for executor tasks

ExecutionAgent (intent: generate_plan)
  → agentic plan generation with ReAct loop + tool calls
  → tools: run_bash (explore environment), ask_assistant (request info from assistant)
  → returns plan with phase="plan_ready" (does NOT execute)

SchedulingAgent receives plan_ready
  → creates plan review task for assistant role
  → dispatches to assistant's execution server

ExecutionAgent (intent: direct_execute) — assistant role
  → reviews plan, provides feedback or approval
  → returns text review

SchedulingAgent processes review
  → if approved: creates execution task with task_type="execute_plan"
  → if feedback: creates revision task with task_type="revise_plan" + review_feedback

ExecutionAgent (intent: execute_plan or revise_plan)
  → executes approved plan step-by-step with ReAct loop
  → or revises plan based on feedback, then returns for re-review
```

**ask_assistant Tool**: During plan generation, execution agents can call `ask_assistant` to request supplementary information from the assistant role:

```
ExecutionAgent calls ask_assistant tool
  → sends ask_assistant_request frame to WS server
  → blocks on PendingAnswerStore (threading.Event)

WS Server receives request
  → creates Task row for assistant
  → dispatches to assistant's server
  → stores correlation: ask_task_id → {requesting_server_id, request_id}

Assistant executes (intent: direct_execute)
  → returns answer via task_result frame

WS Server routes response
  → sends ask_assistant_response frame to requesting server
  → ExecutionAgent's ws_client resolves PendingAnswerStore
  → ask_assistant tool returns answer to LLM
```

**Assistant Role Detection**: Roles are identified as "assistants" via semantic LLM analysis of role definitions — no new configuration fields needed. Characteristics include: reviewing plans, providing feedback, human-in-the-loop interaction. Results are cached per scenario.

**Intent Detection**: Execution agents detect task intent from context:

| Intent | Trigger | Behavior |
|--------|---------|----------|
| `generate_plan` | Default, or `task_type="generate_plan"` | Agentic plan generation with tools |
| `execute_plan` | `task_type="execute_plan"` or context has `plan` | Execute approved plan step-by-step |
| `revise_plan` | `task_type="revise_plan"` or context has `plan` + `review_feedback` | Revise plan based on feedback |
| `direct_execute` | `task_type="direct_execute"` | General-purpose ReAct execution (no planning) |

## Key Properties

- **Single source of truth**: ws_server owns all business data. platform has only local events/messages/human_tasks tables synced via `/subscribe`.
- **Presentation-only platform**: all Gradio pages query ws_server REST API or local tables. No business logic in platform.
- **Ack-on-receipt**: agents ACK immediately on receiving a TASK frame. Crash before ACK → re-send on next heartbeat.
- **Same protocol for all agents**: execution-agent servers and human-agent clients use identical WS frames. Routing is by `server_id`.
- **Event-driven sync**: WSEventSubscriber listens on `/subscribe`, stores all events in local events/messages/human_tasks tables. Dashboard, Event Log, and Flask API read from these tables.
- **Multi-tenant isolation**: all data (ws_server 12 tables + platform 8 local tables + WS broadcasts) scoped by `tenant_id`, enforced at REST, WS, and DB layers. Platform derives tenant context from its `WS_SERVER_API_KEY` JWT at startup.
- **Workflow reuse**: completed scenarios can be published as parameterized DAG workflows and re-executed with new inputs.
- **Plan-first execution**: when assistant roles are detected, execution agents generate plans for review before executing. Plans can be revised based on assistant feedback.
- **Agentic tool use**: execution agents use tools (bash, ask_assistant) during both planning and execution phases via ReAct loops.

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

### Platform-Side Tenant Isolation

The platform is **single-tenant per deployment**: each instance holds one JWT (`WS_SERVER_API_KEY`) and extracts its `tenantId` at startup via `core/local_tenant.py`. All 8 local tables (events, messages, human_tasks, agents, scenarios, tasks, assistant_messages, users) include a `tenant_id` column.

- `database/__init__.py::_migrate_tenant_id()` — auto-runs at startup: ALTER TABLE + index creation + backfill NULL rows with the current tenant
- `BaseRepository` — auto-injects `tenant_id` on `create()`, appends tenant filter on all `find_*` / `count` / `delete` queries (compat mode: includes NULL rows so pre-migration data stays visible)
- `HumanAgentClient` — derives `server_id` from tenant_id (`human-{tenant_id}`) if not explicitly configured
- `WSEventSubscriber` — stores incoming events with tenant_id from the WS broadcast payload

### Migration

`database/migration_tenant.py` (ws_server) provides:
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
│   │   │   ├── agents/            # SchedulingAgent (assistant mode, plan review flow)
│   │   │   ├── ws_protocol.py     # WS frame types (incl. ask_assistant_request/response)
│   │   │   └── ...
│   │   ├── database/              # models, repositories, migration_tenant
│   │   ├── services/              # ScenarioManager, SceneAssistant, WorkflowPublisher/Executor
│   │   └── websocket_server/      # API routes, WS hub (ask_assistant routing), event broadcast
│   ├── agent_server_platform/     # presentation layer: Gradio UI + event sync + human-agent relay
│   └── execution_agent_server/    # sandboxed LLM task runner (Docker)
│       ├── execution_server/      # WS client, task runner, server entry point
│       ├── core/
│       │   ├── agents/
│       │   │   └── execution_agent.py  # Intent detection, agentic planning, plan revision
│       │   ├── pending_answer.py       # Thread-safe blocking store for ask_assistant
│       │   ├── tool_registry.py        # Generic tool registry + OpenAI format conversion
│       │   ├── tools.py                # BashTool, AskAssistantTool
│       │   └── ws_protocol.py          # WS frame types (execution server side)
│       └── ...
├── scripts/
│   ├── build/                     # Dockerfiles per component
│   └── deploy/                    # deploy.sh per component + TLS cert management
├── docs/
│   ├── WEBSOCKET_SERVER_API_KEY_AUTH_DESIGN.md
│   ├── asp-scenario-assistant-design.md
│   └── workflow-system-design.md
├── README.md
└── .gitignore
```

## Components

| Component | Path | Role | Port |
|---|---|---|---|
| **websocket_server** | `src/websocket_server/` | **Core backend**: 12 DB tables, JWT auth, multi-tenant isolation, SchedulingAgent (assistant mode, plan review, semantic role detection), ScenarioManager, SceneAssistant, WorkflowPublisher/Executor, CentralDispatcher, REST API (38 endpoints), WS hub (ask_assistant routing), event broadcast | 8765 (WS + REST) |
| **agent_server_platform** | `src/agent_server_platform/` | **Presentation layer**: Gradio UI (Dashboard, Scenario Dashboard, Workflow DAG, Task Monitor, Agent Registry, Execution Servers, Human Tasks, Event Log), Flask internal API (:5000 with Swagger), HumanAgentClient, WSEventSubscriber, api_client | 8080 (Gradio) + 5000 (Flask) |
| **execution_agent_server** | `src/execution_agent_server/` | Sandboxed LLM task runner; intent-aware execution (generate/execute/revise plan, direct execute), agentic planning with tools (run_bash, ask_assistant), ReAct loops | - (outbound WS) |

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

# 3. Agent server platform (presentation) — Gradio :8080 + Flask :5000
cd src/agent_server_platform
cp .env.example .env
pip install -r requirements.txt
python src/app.py --all
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

Each component has its own `.env` file (copy from `.env.example`).

### websocket_server

| Var | Default | Meaning |
|---|---|---|
| `WS_HOST` | `0.0.0.0` | Bind address |
| `WS_PORT` | `8765` | WS + REST listen port |
| `HEARTBEAT_INTERVAL` | `5` | Agent heartbeat interval (seconds) |
| `HEARTBEAT_TIMEOUT` | `15` | Agent heartbeat timeout (seconds) |
| `WS_SSL_CERT` / `WS_SSL_KEY` | — | TLS cert/key paths (enables WSS+HTTPS on single port) |
| `WS_JWT_SECRET` | `ws-platform-jwt-secret-2026` | HMAC-SHA256 signing key (must match platform) |
| `WS_TENANT_STRICT` | `false` | `true` = strict tenant isolation, `false` = backward-compatible |
| `DB_ENGINE` | `mysql` | Database engine (`mysql`, `sqlite`, `postgresql`) |
| `DB_HOST` / `DB_PORT` | `localhost` / `3306` | Database host and port |
| `DB_NAME` | — | Database name |
| `DB_USER` / `DB_PASSWORD` | — | Database credentials |
| `DASHSCOPE_API_KEY` | — | LLM API key (chat/scheduling) |
| `LLM_BASE_URL` | — | LLM API base URL |
| `LLM_MODEL` | — | LLM model name (e.g. `qwen3.7-plus`) |
| `LLM_MAX_TOKENS` | — | Max tokens per LLM call |
| `LLM_ENABLE_THINKING` | — | Enable thinking mode |
| `LLM_TIMEOUT` | — | LLM request timeout (seconds) |

### agent_server_platform

| Var | Default | Meaning |
|---|---|---|
| `GRADIO_HOST` / `GRADIO_PORT` | `0.0.0.0` / `8080` | Gradio UI bind address |
| `FLASK_HOST` / `FLASK_PORT` | `0.0.0.0` / `5000` | Flask internal API bind address |
| `AUTH_ENABLED` | — | Enable authentication |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | — | Admin backdoor credentials (emergency login) |
| `WS_SERVER_API_URL` | — | HTTP base for ws_server REST API (e.g. `https://host:8765`) |
| `WS_SERVER_WS_URL` | — | WS URL for HumanAgentClient and WSEventSubscriber (e.g. `wss://host:8765`) |
| `DB_ENGINE` | `sqlite` | Local database engine (default: SQLite) |
| `DB_NAME` | `agent_server.db` | Local database file/name |
| `DB_MAX_CONNECTIONS` | — | Connection pool max size |
| `DB_MIN_CONNECTIONS` | — | Connection pool min size |
| `DB_TIMEOUT` | — | Connection pool timeout |
| `DASHSCOPE_API_KEY` | — | LLM API key (compression, local exec) |
| `LLM_BASE_URL` / `LLM_MODEL` | — | LLM API config |
| `LLM_MAX_TOKENS` / `LLM_ENABLE_THINKING` / `LLM_TIMEOUT` | — | LLM tuning |
| `HUMAN_AGENT_QUOTA` | — | Max concurrent human agent tasks |
| `HUMAN_WS_PORT` | — | Human agent browser WebSocket port |
| `SANDBOX_SSH_HOST` | — | SSH host for Docker sandbox management |
| `SANDBOX_DOCKER_IMAGE` | — | Docker image for sandbox containers |
| `SANDBOX_TIMEOUT` / `SANDBOX_MAX_OUTPUT_SIZE` | — | Sandbox limits |
| `WATCHDOG_CHECK_INTERVAL` | — | Watchdog check interval (seconds) |
| `WATCHDOG_DEFAULT_TIMEOUT` | — | Watchdog default timeout |
| `LOG_LEVEL` / `LOG_FORMAT` | — | Logging config |

### execution_agent_server

| Var | Default | Meaning |
|---|---|---|
| `SERVER_ID` | — | Unique server identifier |
| `SERVER_NAME` | — | Display name |
| `MAX_QUOTA` | — | Max concurrent tasks |
| `BACKEND_WS_URL` | — | ws_server WS URL to connect to |
| `HEARTBEAT_INTERVAL` | `5` | Heartbeat interval (seconds) |
| `DASHSCOPE_API_KEY` | — | LLM API key |
| `LLM_BASE_URL` / `LLM_MODEL` | — | LLM API config |
| `LLM_MAX_TOKENS` / `LLM_ENABLE_THINKING` / `LLM_TIMEOUT` | — | LLM tuning |
| `DB_ENGINE` / `DB_NAME` | — | Local database config (events/messages) |
| `LOG_LEVEL` / `LOG_FORMAT` | — | Logging config |

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