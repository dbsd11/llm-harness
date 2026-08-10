# Agent Server Platform - API Documentation

**Base URL**: `http://localhost:5000`  
**API Documentation**: `http://localhost:5000/docs` (Swagger UI)  
**Version**: 1.0.0

---

## Overview

The Agent Server Platform provides a RESTful API for managing agents, tasks, scenarios, and events. The API follows REST conventions and returns JSON responses.

### Authentication

Currently, the API does not require authentication. In production deployments, authentication should be added.

### Content Type

All requests should use `Content-Type: application/json`.

### Response Format

All responses are JSON objects with the following structure:

**Success Response**:
```json
{
  "status": "success",
  "data": { ... }
}
```

**Error Response**:
```json
{
  "status": "error",
  "error": "Error message",
  "code": 400
}
```

---

## Endpoints

### Tasks

#### Create Task

Create a new task for execution.

**Endpoint**: `POST /api/task`

**Request**:
```bash
curl -X POST http://localhost:5000/api/task \
  -H "Content-Type: application/json" \
  -d '{
    "goal": "Analyze data and generate report",
    "agent_type": "scheduling",
    "priority": 1,
    "timeout_seconds": 3600,
    "context": {
      "data_source": "sales_2024.csv"
    }
  }'
```

**Response** (201 Created):
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "created"
}
```

**Request Body**:
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| goal | string | Yes | Task goal description |
| agent_type | string | No | Agent type (scheduling/execution), default: scheduling |
| priority | integer | No | Task priority (0-10), default: 0 |
| timeout_seconds | integer | No | Task timeout in seconds, default: 3600 |
| context | object | No | Additional context for task execution |

---

#### Get Task

Get task details and status.

**Endpoint**: `GET /api/task/{task_id}`

**Request**:
```bash
curl http://localhost:5000/api/task/550e8400-e29b-41d4-a716-446655440000
```

**Response** (200 OK):
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "goal": "Analyze data and generate report",
  "state": "running",
  "agent_type": "scheduling",
  "priority": 1,
  "timeout_seconds": 3600,
  "context": {
    "data_source": "sales_2024.csv"
  },
  "result": null,
  "error": null,
  "created_at": "2026-07-21T10:30:00",
  "updated_at": "2026-07-21T10:30:05",
  "started_at": "2026-07-21T10:30:05",
  "completed_at": null
}
```

**Response Fields**:
| Field | Type | Description |
|-------|------|-------------|
| task_id | string | Unique task identifier |
| goal | string | Task goal description |
| state | string | Task state (pending/running/success/failed/timeout/cancelled) |
| agent_type | string | Agent type handling the task |
| priority | integer | Task priority |
| timeout_seconds | integer | Task timeout |
| context | object | Task context |
| result | object/null | Task result (if completed) |
| error | string/null | Error message (if failed) |
| created_at | string | Creation timestamp |
| updated_at | string | Last update timestamp |
| started_at | string/null | Start timestamp |
| completed_at | string/null | Completion timestamp |

**Task States**:
- `pending`: Task created, waiting to start
- `running`: Task is executing
- `success`: Task completed successfully
- `failed`: Task failed with error
- `timeout`: Task timed out
- `cancelled`: Task was cancelled

---

#### List Tasks

List all tasks with optional filtering.

**Endpoint**: `GET /api/tasks`

**Query Parameters**:
| Parameter | Type | Description |
|-----------|------|-------------|
| state | string | Filter by state (pending/running/success/failed/timeout/cancelled) |
| limit | integer | Maximum number of tasks to return, default: 100 |
| offset | integer | Number of tasks to skip, default: 0 |

**Request**:
```bash
curl "http://localhost:5000/api/tasks?state=running&limit=10"
```

**Response** (200 OK):
```json
{
  "tasks": [
    {
      "task_id": "550e8400-e29b-41d4-a716-446655440000",
      "goal": "Analyze data",
      "state": "running",
      "created_at": "2026-07-21T10:30:00"
    }
  ],
  "total": 1
}
```

---

#### Cancel Task

Cancel a running task.

**Endpoint**: `DELETE /api/task/{task_id}`

**Request**:
```bash
curl -X DELETE http://localhost:5000/api/task/550e8400-e29b-41d4-a716-446655440000
```

**Response** (200 OK):
```json
{
  "status": "cancelled",
  "task_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

---

### Scenarios

#### Create Scenario

Create a new scenario.

**Endpoint**: `POST /api/scenario`

**Request**:
```bash
curl -X POST http://localhost:5000/api/scenario \
  -H "Content-Type: application/json" \
  -d '{
    "scenario_type": "simple_qa",
    "name": "QA Scenario",
    "description": "Question-answering scenario",
    "config": {
      "question": "What is the capital of France?"
    }
  }'
```

**Response** (201 Created):
```json
{
  "scenario_id": "660e8400-e29b-41d4-a716-446655440001",
  "status": "created"
}
```

**Request Body**:
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| scenario_type | string | Yes | Scenario type (simple_qa/code_execution) |
| name | string | Yes | Scenario name |
| description | string | No | Scenario description |
| config | object | No | Scenario configuration |

**Scenario Types**:
- `simple_qa`: Question-answering workflow
- `code_execution`: Code execution in sandbox

---

#### Get Scenario

Get scenario details and status.

**Endpoint**: `GET /api/scenario/{scenario_id}`

**Request**:
```bash
curl http://localhost:5000/api/scenario/660e8400-e29b-41d4-a716-446655440001
```

**Response** (200 OK):
```json
{
  "scenario_id": "660e8400-e29b-41d4-a716-446655440001",
  "scenario_type": "simple_qa",
  "name": "QA Scenario",
  "description": "Question-answering scenario",
  "state": "initializing",
  "config": {
    "question": "What is the capital of France?"
  },
  "context": {},
  "created_at": "2026-07-21T10:30:00",
  "updated_at": "2026-07-21T10:30:00",
  "started_at": null,
  "completed_at": null
}
```

**Scenario States**:
- `initializing`: Scenario created, not started
- `running`: Scenario is executing
- `completed`: Scenario completed successfully
- `failed`: Scenario failed with error
- `cancelled`: Scenario was cancelled

---

#### Start Scenario

Start scenario execution.

**Endpoint**: `POST /api/scenario/{scenario_id}/start`

**Request**:
```bash
curl -X POST http://localhost:5000/api/scenario/660e8400-e29b-41d4-a716-446655440001/start
```

**Response** (200 OK):
```json
{
  "status": "started",
  "scenario_id": "660e8400-e29b-41d4-a716-446655440001"
}
```

---

#### List Scenarios

List all scenarios.

**Endpoint**: `GET /api/scenarios`

**Query Parameters**:
| Parameter | Type | Description |
|-----------|------|-------------|
| state | string | Filter by state |
| limit | integer | Maximum number of scenarios, default: 100 |

**Request**:
```bash
curl "http://localhost:5000/api/scenarios?state=running"
```

**Response** (200 OK):
```json
{
  "scenarios": [
    {
      "scenario_id": "660e8400-e29b-41d4-a716-446655440001",
      "scenario_type": "simple_qa",
      "name": "QA Scenario",
      "state": "running",
      "created_at": "2026-07-21T10:30:00"
    }
  ],
  "total": 1
}
```

---

#### Cancel Scenario

Cancel a running scenario.

**Endpoint**: `DELETE /api/scenario/{scenario_id}`

**Request**:
```bash
curl -X DELETE http://localhost:5000/api/scenario/660e8400-e29b-41d4-a716-446655440001
```

**Response** (200 OK):
```json
{
  "status": "cancelled",
  "scenario_id": "660e8400-e29b-41d4-a716-446655440001"
}
```

---

### Agents

#### Register Agent

Register a new agent.

**Endpoint**: `POST /api/agent`

**Request**:
```bash
curl -X POST http://localhost:5000/api/agent \
  -H "Content-Type: application/json" \
  -d '{
    "agent_type": "scheduling",
    "name": "My Scheduling Agent",
    "description": "Handles task scheduling",
    "config": {
      "max_concurrent_tasks": 5
    }
  }'
```

**Response** (201 Created):
```json
{
  "agent_id": "770e8400-e29b-41d4-a716-446655440002",
  "status": "registered"
}
```

**Request Body**:
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| agent_type | string | Yes | Agent type (scheduling/execution) |
| name | string | Yes | Agent name |
| description | string | No | Agent description |
| config | object | No | Agent configuration |

---

#### List Agents

List all registered agents.

**Endpoint**: `GET /api/agents`

**Request**:
```bash
curl http://localhost:5000/api/agents
```

**Response** (200 OK):
```json
{
  "agents": [
    {
      "agent_id": "770e8400-e29b-41d4-a716-446655440002",
      "agent_type": "scheduling",
      "name": "My Scheduling Agent",
      "status": "active",
      "created_at": "2026-07-21T10:30:00"
    }
  ],
  "total": 1
}
```

---

### Events

#### List Events

List system events.

**Endpoint**: `GET /api/events`

**Query Parameters**:
| Parameter | Type | Description |
|-----------|------|-------------|
| event_type | string | Filter by event type |
| limit | integer | Maximum number of events, default: 100 |

**Request**:
```bash
curl "http://localhost:5000/api/events?event_type=task.created&limit=50"
```

**Response** (200 OK):
```json
{
  "events": [
    {
      "event_id": "880e8400-e29b-41d4-a716-446655440003",
      "event_type": "task.created",
      "data": {
        "task_id": "550e8400-e29b-41d4-a716-446655440000",
        "goal": "Analyze data"
      },
      "timestamp": "2026-07-21T10:30:00"
    }
  ],
  "total": 1
}
```

**Event Types**:
- `task.created`: Task created
- `task.started`: Task started execution
- `task.completed`: Task completed successfully
- `task.failed`: Task failed
- `task.state_changed`: Task state changed
- `scenario.created`: Scenario created
- `scenario.started`: Scenario started
- `scenario.completed`: Scenario completed
- `scenario.failed`: Scenario failed
- `scenario.state_changed`: Scenario state changed
- `agent.registered`: Agent registered
- `agent.started`: Agent started
- `agent.stopped`: Agent stopped

---

## Error Codes

| Code | Description |
|------|-------------|
| 200 | Success |
| 201 | Created |
| 400 | Bad Request - Invalid request body or parameters |
| 404 | Not Found - Resource not found |
| 409 | Conflict - Resource already exists |
| 500 | Internal Server Error - Unexpected error |

---

## Examples

### Complete Workflow Example

```bash
# 1. Create a scenario
SCENARIO_ID=$(curl -X POST http://localhost:5000/api/scenario \
  -H "Content-Type: application/json" \
  -d '{
    "scenario_type": "simple_qa",
    "name": "QA Test",
    "config": {"question": "What is 2+2?"}
  }' | jq -r '.scenario_id')

echo "Created scenario: $SCENARIO_ID"

# 2. Start the scenario
curl -X POST http://localhost:5000/api/scenario/$SCENARIO_ID/start

# 3. Check scenario status
sleep 2
curl http://localhost:5000/api/scenario/$SCENARIO_ID | jq

# 4. List events
curl http://localhost:5000/api/events?limit=10 | jq
```

### Task Submission Example

```bash
# Submit a task
TASK_ID=$(curl -X POST http://localhost:5000/api/task \
  -H "Content-Type: application/json" \
  -d '{
    "goal": "Process sales data",
    "agent_type": "scheduling",
    "priority": 5,
    "timeout_seconds": 300
  }' | jq -r '.task_id')

echo "Submitted task: $TASK_ID"

# Monitor task status
while true; do
  STATUS=$(curl http://localhost:5000/api/task/$TASK_ID | jq -r '.state')
  echo "Task state: $STATUS"
  
  if [ "$STATUS" = "success" ] || [ "$STATUS" = "failed" ]; then
    break
  fi
  
  sleep 2
done

# Get task result
curl http://localhost:5000/api/task/$TASK_ID | jq '.result'
```

---

## Rate Limiting

Currently, there is no rate limiting. In production deployments, rate limiting should be implemented to prevent abuse.

---

## CORS

CORS is enabled for all origins in development. In production, CORS should be restricted to specific origins.

---

## API Versioning

The API version is included in the response headers:

```
X-API-Version: 1.0.0
```

---

## Support

For API issues or questions:
- GitHub Issues: [Project Repository]
- Email: support@example.com

---

**Last Updated**: 2026/07/21  
**API Version**: 1.0.0
