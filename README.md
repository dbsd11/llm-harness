# LLM Harness

A unified, distributed agent-server platform for multi-agent orchestration.
Three peers collaborate over WebSocket/HTTP:

- a **websocket_server** (core backend: REST API + WS hub + scheduling/scenario engine + scene assistant + event broadcast),
- an **agent_server_platform** (presentation layer: Gradio UI + event sync + human-agent relay), and
- one or more **execution_agent_server** (sandboxed LLM task runners).

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  websocket_server (core backend, aiohttp :8765)                     │
│                                                                      │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌───────────┐  │
│  │Scheduling    │ │Scenario      │ │SceneAssistant│ │REST API   │  │
│  │Agent         │ │Manager       │ │(chatbot)     │ │17 endpoints│  │
│  │decompose+    │ │create+start+ │ │/api/chat     │ │scenarios,  │  │
│  │dispatch      │ │stop+review   │ │              │ │tasks,agents│  │
│  └──────┬───────┘ └──────┬───────┘ └──────┬───────┘ │events,chat │  │
│         │                │                │         └─────┬───────┘  │
│  ┌──────┴────────────────┴────────────────┴───────────────┴───────┐ │
│  │                    CentralDispatcher                           │ │
│  │  polls dispatch rows (acked=0) → routes to WS or local exec    │ │
│  └──────────────────────────┬─────────────────────────────────────┘ │
│                             │                                        │
│  ┌──────────────────────────┴─────────────────────────────────────┐ │
│  │              WS Hub + Event Broadcast                          │ │
│  │  / (agent connections)  /subscribe (event stream)              │ │
│  │  register/heartbeat/task/task_result/ack                       │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                                                                      │
│  Owns all 10 DB tables: scenarios, tasks, events, messages,          │
│  agents, users, execution_servers, human_tasks, assistant_messages,  │
│  consumer_offsets                                                    │
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
│  │ Task Monitor       │  │ beat/recv TASK   │  │ local events    │   │
│  │ Agent Registry     │  │ send task_result │  │ table           │   │
│  │ Human Tasks        │  │                  │  │                 │   │
│  │ Event Log          │  └────────┬─────────┘  └────────┬────────┘   │
│  │                    │           │                      │            │
│  │ All writes via     │           │                      │            │
│  │ api_client →       │           │                      │            │
│  │ ws_server REST     │           │                      │            │
│  └────────────────────┘           │                      │            │
│                                    │                      │            │
│  Local DB: events table only       │                      │            │
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
```

## Key Properties

- **Single source of truth**: ws_server owns all business data. platform has only a local events table synced via `/subscribe`.
- **Presentation-only platform**: all Gradio pages query ws_server REST API or local events table. No business logic in platform.
- **Ack-on-receipt**: agents ACK immediately on receiving a TASK frame. Crash before ACK → re-send on next heartbeat.
- **Same protocol for all agents**: execution-agent servers and human-agent clients use identical WS frames. Routing is by `server_id`.
- **Event-driven sync**: WSEventSubscriber listens on `/subscribe`, stores all events in local events table. Dashboard, Event Log, and Flask API read from this table.

## Repository Layout

```
llm-harness/
├── src/
│   ├── websocket_server/          # core backend: aiohttp WS+REST hub + all business logic
│   ├── agent_server_platform/     # presentation layer: Gradio UI + event sync + human-agent relay
│   └── execution_agent_server/    # sandboxed LLM task runner (Docker)
├── scripts/
│   ├── build/                     # Dockerfiles per component
│   └── deploy/                    # deploy.sh per component
├── docs/
│   └── design/                    # architecture and refactoring design docs
├── README.md
└── .gitignore
```

## Components

| Component | Path | Role | Port |
|---|---|---|---|
| **websocket_server** | `src/websocket_server/` | **Core backend**: 10 DB tables, SchedulingAgent, ScenarioManager, SceneAssistant, CentralDispatcher, REST API (17 endpoints), WS hub, event broadcast | 8765 (WS + REST) |
| **agent_server_platform** | `src/agent_server_platform/` | **Presentation layer**: Gradio UI (Dashboard, Scenario Dashboard, Task Monitor, Agent Registry, Human Tasks, Event Log), HumanAgentClient, WSEventSubscriber, api_client | 8080 (Gradio) |
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
| `WS_SERVER_API_URL` | platform `.env` | HTTP base for ws_server REST API (e.g. `http://127.0.0.1:8765`) |
| `WS_SERVER_WS_URL` | platform `.env` | WS URL for HumanAgentClient and WSEventSubscriber |
| `DASHSCOPE_API_KEY` | platform + ws_server `.env` | LLM API key (both need it: platform for compression, ws_server for chat/scheduling) |
| `LLM_BASE_URL` | platform + ws_server `.env` | LLM API base URL |
| `LLM_MODEL` | platform + ws_server `.env` | LLM model name (e.g. `qwen3.7-plus`) |

## ws_server REST API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check |
| GET/POST/DELETE | `/api/scenarios[/{id}]` | Scenario CRUD |
| POST | `/api/scenarios/{id}/start` | Start scenario |
| POST | `/api/scenarios/{id}/stop` | Stop scenario |
| GET | `/api/scenarios/{id}/messages` | Agent communication timeline |
| GET/DELETE | `/api/tasks[/{id}]` | Task list / cancel |
| POST | `/api/tasks/{id}/accept` | Human acceptance review |
| POST | `/api/chat` | Scene assistant chatbot |
| POST | `/api/chat/save` | Save scene spec |
| GET | `/api/chat/preview` | Get scene preview |
| GET | `/api/chat/load-scene` | Load existing scene for editing |
| GET | `/api/chat/scene-index` | Get scene list for assistant |
| GET | `/api/agents[/{id}]` | Agent registry |
| GET/POST | `/api/events` | Event query / create |
| GET | `/api/servers[/{id}]` | Execution server registry |
| POST | `/api/servers/offline` | Cleanup offline servers |
| POST | `/api/tasks/dispatch` | Internal task dispatch |

## License

Internal project.