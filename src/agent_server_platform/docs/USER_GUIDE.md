# Agent Server Platform - User Guide

Welcome to the Agent Server Platform! This guide will help you get started with the system.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Architecture Overview](#architecture-overview)
3. [Core Concepts](#core-concepts)
4. [Using the Gradio UI](#using-the-gradio-ui)
5. [Using the REST API](#using-the-rest-api)
6. [Common Workflows](#common-workflows)
7. [Troubleshooting](#troubleshooting)
8. [Best Practices](#best-practices)

---

## Quick Start

### Prerequisites

- Python 3.10+
- pip

### Installation

```bash
# Clone the repository
cd agent-server-platform

# Install dependencies
pip install -r requirements.txt

# Copy environment configuration
cp .env.example .env
```

### Start the Platform

```bash
# Start both Gradio UI and Flask API
python src/app.py --all
```

### Access the Platform

- **Gradio UI**: http://localhost:8080
- **Flask API**: http://localhost:5000
- **API Documentation**: http://localhost:5000/docs

---

## Architecture Overview

The Agent Server Platform consists of three main layers:

### Layer 1: Universal Agent Platform

- **SchedulingAgent**: Decomposes goals into tasks, coordinates execution
- **ExecutionAgent**: Executes tasks in sandbox environment
- **A2A Protocol**: Agent-to-agent communication
- **Event System**: Event-driven architecture for state changes
- **Watchdog**: Timeout detection and recovery

### Layer 2: Scenario Collaboration Platform

- **Scenario Manager**: Manages scenario lifecycle
- **Scenario Types**: 
  - SimpleQAScenario: Question-answering workflows
  - CodeExecutionScenario: Code execution in sandbox

### Infrastructure

- **Database**: SQLite (dev) / MySQL (prod)
- **Event Bus**: Queue-based with DB persistence
- **Sandbox**: Subprocess isolation (evolves to Docker)
- **State Machines**: Task and Scenario state management

---

## Core Concepts

### Tasks

A **Task** represents a unit of work to be executed by an agent.

**Task States**:
- `pending`: Created, waiting to start
- `running`: Currently executing
- `success`: Completed successfully
- `failed`: Failed with error
- `timeout`: Timed out
- `cancelled`: Cancelled by user

**Example**:
```python
task = {
    "goal": "Analyze sales data and generate report",
    "agent_type": "scheduling",
    "priority": 5,
    "timeout_seconds": 3600
}
```

### Scenarios

A **Scenario** represents a complete workflow that may involve multiple tasks.

**Scenario States**:
- `initializing`: Created, not started
- `running`: Currently executing
- `completed`: Finished successfully
- `failed`: Failed with error
- `cancelled`: Cancelled by user

**Example**:
```python
scenario = {
    "scenario_type": "simple_qa",
    "name": "Q&A Session",
    "config": {
        "question": "What is machine learning?"
    }
}
```

### Agents

An **Agent** is an autonomous entity that executes tasks.

**Agent Types**:
- `scheduling`: Decomposes goals into tasks
- `execution`: Executes code in sandbox

**Example**:
```python
agent = {
    "agent_type": "scheduling",
    "name": "My Agent",
    "config": {
        "max_concurrent_tasks": 5
    }
}
```

### Events

An **Event** represents a state change in the system.

**Common Events**:
- `task.created`: Task was created
- `task.started`: Task started execution
- `task.completed`: Task completed successfully
- `task.failed`: Task failed
- `scenario.created`: Scenario was created
- `scenario.started`: Scenario started execution
- `scenario.completed`: Scenario completed

---

## Using the Gradio UI

### Home Page

The home page shows system statistics:
- Number of registered agents
- Total number of tasks
- Number of running tasks
- Number of scenarios
- Recent events

### Agent Registry

**Register an Agent**:

1. Navigate to "Agent Registry" page
2. Fill in the form:
   - Agent Type: scheduling or execution
   - Agent Name: Descriptive name
   - Description: What the agent does
   - Config: JSON configuration (optional)
3. Click "Register Agent"

**View Registered Agents**:
- The agent list shows all registered agents
- Each agent shows: ID, type, name, status, creation time

### Task Monitor

**Submit a Task**:

1. Navigate to "Task Monitor" page
2. Fill in the form:
   - Goal: What you want to achieve
   - Agent Type: scheduling or execution
   - Priority: 0-10 (higher = more important)
   - Timeout: Maximum execution time (seconds)
   - Context: JSON context (optional)
3. Click "Submit Task"

**Monitor Tasks**:
- Task list shows all tasks with their states
- Click on a task to see details
- Tasks auto-refresh every 5 seconds

**Filter Tasks**:
- Use the state filter to show only tasks in a specific state
- Click "Refresh" to manually refresh the list

### Scenario Dashboard

**Create a Scenario**:

1. Navigate to "Scenario Dashboard" page
2. Fill in the form:
   - Scenario Type: simple_qa or code_execution
   - Scenario Name: Descriptive name
   - Description: What the scenario does
   - Config: JSON configuration
3. Click "Create Scenario"

**Start a Scenario**:
- Enter the scenario ID
- Click "Start Scenario"

**Monitor Scenarios**:
- Scenario list shows all scenarios with their states
- Click on a scenario to see details

### Event Log

**View Events**:
- Event log shows all system events
- Events auto-refresh every 2 seconds
- Filter by event type using the dropdown

**Event Types**:
- `task.*`: All task-related events
- `scenario.*`: All scenario-related events
- `agent.*`: All agent-related events
- `watchdog.*`: All watchdog events

---

## Using the REST API

### Authentication

Currently, no authentication is required. In production, add authentication.

### Base URL

```
http://localhost:5000
```

### Common Operations

#### Create a Task

```bash
curl -X POST http://localhost:5000/api/task \
  -H "Content-Type: application/json" \
  -d '{
    "goal": "Analyze data and generate report",
    "agent_type": "scheduling",
    "priority": 5,
    "timeout_seconds": 3600
  }'
```

**Response**:
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "created"
}
```

#### Get Task Status

```bash
curl http://localhost:5000/api/task/550e8400-e29b-41d4-a716-446655440000
```

**Response**:
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "goal": "Analyze data and generate report",
  "state": "running",
  "agent_type": "scheduling",
  "priority": 5,
  "timeout_seconds": 3600,
  "created_at": "2026-07-21T10:30:00",
  "started_at": "2026-07-21T10:30:05"
}
```

#### Create a Scenario

```bash
curl -X POST http://localhost:5000/api/scenario \
  -H "Content-Type: application/json" \
  -d '{
    "scenario_type": "simple_qa",
    "name": "Q&A Test",
    "config": {
      "question": "What is the capital of France?"
    }
  }'
```

**Response**:
```json
{
  "scenario_id": "660e8400-e29b-41d4-a716-446655440001",
  "status": "created"
}
```

#### Start a Scenario

```bash
curl -X POST http://localhost:5000/api/scenario/660e8400-e29b-41d4-a716-446655440001/start
```

**Response**:
```json
{
  "status": "started",
  "scenario_id": "660e8400-e29b-41d4-a716-446655440001"
}
```

#### List Events

```bash
curl "http://localhost:5000/api/events?event_type=task.created&limit=50"
```

**Response**:
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
  ]
}
```

### API Documentation

Visit http://localhost:5000/docs for interactive API documentation (Swagger UI).

---

## Common Workflows

### Workflow 1: Simple Task Execution

**Goal**: Execute a simple task and monitor its progress.

**Steps**:

1. **Submit Task**:
   ```bash
   TASK_ID=$(curl -X POST http://localhost:5000/api/task \
     -H "Content-Type: application/json" \
     -d '{"goal": "Calculate 2+2", "agent_type": "execution"}' \
     | jq -r '.task_id')
   ```

2. **Monitor Progress**:
   ```bash
   watch -n 2 "curl -s http://localhost:5000/api/task/$TASK_ID | jq '.state'"
   ```

3. **Get Result**:
   ```bash
   curl http://localhost:5000/api/task/$TASK_ID | jq '.result'
   ```

### Workflow 2: Scenario-Based Execution

**Goal**: Create and execute a complete scenario.

**Steps**:

1. **Create Scenario**:
   ```bash
   SCENARIO_ID=$(curl -X POST http://localhost:5000/api/scenario \
     -H "Content-Type: application/json" \
     -d '{
       "scenario_type": "simple_qa",
       "name": "Math Q&A",
       "config": {"question": "What is 10 * 5?"}
     }' | jq -r '.scenario_id')
   ```

2. **Start Scenario**:
   ```bash
   curl -X POST http://localhost:5000/api/scenario/$SCENARIO_ID/start
   ```

3. **Monitor Scenario**:
   ```bash
   watch -n 2 "curl -s http://localhost:5000/api/scenario/$SCENARIO_ID | jq '.state'"
   ```

4. **Get Result**:
   ```bash
   curl http://localhost:5000/api/scenario/$SCENARIO_ID | jq
   ```

### Workflow 3: Batch Task Submission

**Goal**: Submit multiple tasks and monitor them.

**Steps**:

1. **Submit Multiple Tasks**:
   ```bash
   for i in {1..5}; do
     curl -X POST http://localhost:5000/api/task \
       -H "Content-Type: application/json" \
       -d "{\"goal\": \"Task $i\", \"priority\": $i}"
   done
   ```

2. **List All Tasks**:
   ```bash
   curl http://localhost:5000/api/tasks | jq '.tasks[] | {task_id, goal, state}'
   ```

3. **Filter by State**:
   ```bash
   curl "http://localhost:5000/api/tasks?state=running" | jq
   ```

### Workflow 4: Event Monitoring

**Goal**: Monitor system events in real-time.

**Steps**:

1. **Watch Events**:
   ```bash
   watch -n 1 "curl -s 'http://localhost:5000/api/events?limit=20' | jq '.events[] | {event_type, timestamp}'"
   ```

2. **Filter by Type**:
   ```bash
   curl "http://localhost:5000/api/events?event_type=task.*&limit=50" | jq
   ```

---

## Troubleshooting

### Task Stuck in RUNNING State

**Problem**: Task stays in RUNNING state indefinitely.

**Solutions**:
1. Check if watchdog is running:
   ```bash
   # Check logs
   tail -f logs/app.log | grep watchdog
   ```

2. Verify task timeout is set:
   ```bash
   curl http://localhost:5000/api/task/$TASK_ID | jq '.timeout_seconds'
   ```

3. Manually cancel the task:
   ```bash
   curl -X DELETE http://localhost:5000/api/task/$TASK_ID
   ```

### Sandbox Execution Fails

**Problem**: Code execution in sandbox fails.

**Solutions**:
1. Check sandbox logs:
   ```bash
   tail -f logs/app.log | grep sandbox
   ```

2. Verify code syntax:
   ```python
   # Test code locally first
   python -c "print('test')"
   ```

3. Increase timeout:
   ```bash
   curl -X POST http://localhost:5000/api/task \
     -d '{"goal": "test", "timeout_seconds": 600}'
   ```

### Event Not Received

**Problem**: Events are not being received by subscribers.

**Solutions**:
1. Check event bus is running:
   ```bash
   tail -f logs/app.log | grep "Event bus"
   ```

2. Verify subscription:
   ```python
   # Check if handler is registered
   print(event_bus.subscribers)
   ```

3. Wait for async processing:
   ```python
   import time
   time.sleep(0.1)  # Wait for event processing
   ```

### Database Locked

**Problem**: SQLite database is locked during concurrent access.

**Solutions**:
1. Reduce concurrent operations
2. Use connection pooling
3. Consider migrating to MySQL:
   ```bash
   # Update .env
   DB_ENGINE=mysql
   DB_HOST=localhost
   DB_PORT=3306
   DB_NAME=agent_server
   ```

### Agent Not Executing

**Problem**: Agent is registered but not executing tasks.

**Solutions**:
1. Check agent is registered:
   ```bash
   curl http://localhost:5000/api/agents | jq
   ```

2. Verify task is submitted:
   ```bash
   curl http://localhost:5000/api/tasks | jq
   ```

3. Check logs for errors:
   ```bash
   tail -f logs/app.log | grep ERROR
   ```

### Gradio UI Not Loading

**Problem**: Gradio UI shows blank page or error.

**Solutions**:
1. Check if Gradio is running:
   ```bash
   ps aux | grep gradio
   ```

2. Check browser console for errors:
   - Open DevTools (F12)
   - Check Console tab

3. Restart Gradio:
   ```bash
   python src/app.py --gradio-only
   ```

---

## Best Practices

### Task Design

**DO**:
- ✅ Set appropriate timeouts (300-3600 seconds)
- ✅ Use priorities to control execution order
- ✅ Provide clear, specific goals
- ✅ Include relevant context in the task

**DON'T**:
- ❌ Set timeouts too short (< 60 seconds)
- ❌ Submit too many high-priority tasks at once
- ❌ Use vague goals like "do something"
- ❌ Forget to include necessary context

### Scenario Design

**DO**:
- ✅ Use the right scenario type for the task
- ✅ Provide complete configuration
- ✅ Monitor scenario progress
- ✅ Handle failures gracefully

**DON'T**:
- ❌ Use simple_qa for code execution
- ❌ Start scenarios without testing config first
- ❌ Ignore scenario state changes
- ❌ Leave scenarios running indefinitely

### Resource Management

**DO**:
- ✅ Clean up completed tasks and scenarios
- ✅ Monitor resource usage
- ✅ Set appropriate limits
- ✅ Use cancellation for long-running tasks

**DON'T**:
- ❌ Let tasks accumulate indefinitely
- ❌ Ignore memory usage
- ❌ Set unlimited timeouts
- ❌ Forget to cancel unnecessary tasks

### Error Handling

**DO**:
- ✅ Check task/scenario state regularly
- ✅ Handle errors in your code
- ✅ Log errors for debugging
- ✅ Implement retry logic

**DON'T**:
- ❌ Ignore error states
- ❌ Assume tasks always succeed
- ❌ Forget to check error messages
- ❌ Retry without fixing the root cause

### Performance

**DO**:
- ✅ Use appropriate priorities
- ✅ Batch similar tasks together
- ✅ Monitor event queue size
- ✅ Use pagination for large lists

**DON'T**:
- ❌ Submit too many tasks at once
- ❌ Poll for status too frequently
- ❌ Fetch all events without limits
- ❌ Ignore performance metrics

---

## Advanced Usage

### Custom Agent Types

You can create custom agent types by extending `BaseAgent`:

```python
from src.core.agents.base_agent import BaseAgent

class MyCustomAgent(BaseAgent):
    def get_agent_type(self):
        return "custom"
    
    def initialize(self, config):
        # Initialize your agent
        pass
    
    def run(self, task_id, context):
        # Execute your logic
        return {"success": True, "result": "done"}
    
    def cleanup(self):
        # Cleanup resources
        pass
```

### Custom Scenario Types

You can create custom scenario types by extending `BaseScenario`:

```python
from src.scenarios.base_scenario import BaseScenario

class MyCustomScenario(BaseScenario):
    def get_scenario_type(self):
        return "custom"
    
    def initialize(self, config):
        # Initialize your scenario
        pass
    
    def run(self):
        # Execute your workflow
        return {"success": True, "result": "done"}
    
    def cleanup(self):
        # Cleanup resources
        pass
```

### Event Subscriptions

Subscribe to events for real-time updates:

```python
from src.core.event_bus import event_bus, EventType

def handle_task_completed(event):
    print(f"Task completed: {event['task_id']}")

event_bus.subscribe(EventType.TASK_COMPLETED, handle_task_completed)
```

---

## Support

For issues or questions:
- GitHub Issues: [Project Repository]
- Email: support@example.com
- Documentation: See `knowledge/README.md` for detailed information

---

## Changelog

### Version 1.0.0 (2026-07-21)
- ✅ Phase 1: Core Infrastructure
- ✅ Phase 2: Agent System
- ✅ Phase 3: Scenario System
- ✅ Phase 4: Testing and Documentation

---

**Last Updated**: 2026/07/21  
**Version**: 1.0.0
