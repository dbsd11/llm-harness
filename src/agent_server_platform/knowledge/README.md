# Agent Server Platform - Knowledge Base

This document contains architecture decisions, patterns, constraints, and guidelines for the Agent Server Platform.

---

## Architecture Decisions

### Why SQLite for Development?

- **Zero external dependencies**: Just Python stdlib, no database server setup required
- **Fast iteration**: No network overhead, file-based storage
- **Easy to test**: Can copy DB files, reset by deleting file
- **Migration path**: BaseModel handles SQLite → MySQL/PostgreSQL automatically
- **When to evolve**: When you need concurrent writes from multiple processes, or production-grade performance

### Why Queue-Based Event Bus?

- **Simple**: Uses stdlib `queue.Queue`, no external message broker
- **Single-process**: No distributed complexity initially
- **Persistence**: Events are persisted to DB before processing
- **Evolution path**: 
  - Phase 1: `queue.Queue` (current - single-process)
  - Phase 2: Redis pub/sub (multi-process)
  - Phase 3: RabbitMQ/Kafka (distributed, cross-cluster)
- **When to evolve**: When deploying multiple Gradio/Flask processes that need to share events

### Why Subprocess Sandbox?

- **Fast**: No container overhead, direct process execution
- **Simple**: Python stdlib `subprocess`, no Docker/K8s knowledge required
- **Adequate for trusted code**: Good for internal tools and verified scripts
- **Evolution path**:
  - Phase 1: `subprocess` (current - simple isolation)
  - Phase 2: Docker containers (strong isolation, resource limits)
  - Phase 3: Kubernetes pods (distributed, scalable)
- **When to evolve**: When executing untrusted code or needing strict resource limits

### Why In-Process A2A Protocol?

- **Simple**: Queue-based messaging, no network complexity
- **Single-process**: All agents in same process, easy to debug
- **Evolution path**:
  - Phase 1: In-process queues (current - single-process)
  - Phase 2: HTTP REST API (multi-process)
  - Phase 3: gRPC (cross-cluster, high-performance)
- **When to evolve**: When agents need to run on different machines or clusters

### Why Enum-Based State Machine?

- **Simple**: stdlib `enum.Enum` + dict-based transition table
- **No dependencies**: No external state machine library
- **Easy to understand**: Explicit transitions, no magic
- **When to evolve**: When transitions exceed 50 states or need complex guards/actions

### Why Config-Driven Gradio Routing?

- **Proven pattern**: Reused from ai-app-template
- **Easy to extend**: Add new pages by updating `routes` list in `router.py`
- **Dynamic loading**: Uses `import_module()`, no hardcoded imports
- **When to evolve**: When you need dynamic route registration or A/B testing

---

## Patterns to Follow

### Database Layer

**Use BaseModel + BaseRepository pattern**:
```python
# Define model
class MyModel(BaseModel):
    __tablename__ = "my_table"
    __primary_key__ = "id"
    __fields__ = {
        "id": int,
        "name": str,
        "created_at": datetime,
    }

# Define repository
class MyRepository(BaseRepository[MyModel]):
    def __init__(self):
        super().__init__(MyModel)
    
    def find_by_name(self, name: str):
        results = self.find_by_criteria({"name": name})
        return results[0] if results else None
```

**Rules**:
- ✅ Always use repositories for database operations
- ❌ Never write raw SQL in application code
- ✅ Use `find_by_criteria()` for complex queries
- ✅ Use `to_dict()` and `from_dict()` for serialization

### Agent Layer

**Use Agent + AgentRun separation**:
- `BaseAgent`: Defines agent behavior (initialize, run, cleanup)
- `AgentRun`: Manages execution instance (state, result, error)

**Rules**:
- ✅ Use `global_loop_util` for async execution
- ✅ Use `SystemMessageOutput` for streaming results
- ✅ Emit events for all state changes
- ✅ Handle errors gracefully, never crash the agent loop

### State Machine

**Use enum + transition table**:
```python
class MyState(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"

MY_STATE_MACHINE = StateMachine({
    MyState.PENDING: {MyState.RUNNING},
    MyState.RUNNING: {MyState.SUCCESS},
    MyState.SUCCESS: set(),  # Terminal
})
```

**Rules**:
- ✅ Always validate transitions with `can_transition()`
- ✅ Emit events on state changes
- ✅ Use terminal states (empty set) for final states

### Event System

**Use EventBus for all inter-component communication**:
```python
from core.event_bus import event_bus

# Emit event
event_bus.emit("task.created", {"task_id": task_id})

# Subscribe to event
def handle_task_created(event):
    print(f"Task created: {event['data']['task_id']}")

event_bus.subscribe("task.created", handle_task_created)
```

**Rules**:
- ✅ Emit events for all state changes
- ✅ Use descriptive event types (e.g., `task.state_changed`)
- ✅ Subscribe using wildcards for related events (e.g., `task.*`)
- ✅ Persist all events to DB (handled by EventBus)

### Watchdog

**Run in background thread, check periodically**:
```python
from core.watchdog import watchdog

# Start watchdog
watchdog.start()

# Watchdog automatically checks for timeouts
```

**Rules**:
- ✅ Set appropriate `timeout_seconds` for tasks
- ✅ Set `max_retries` for retry logic
- ✅ Monitor watchdog events for debugging

---

## Constraints

### What NOT to Do

- ❌ **Don't add external dependencies without justification**: Use stdlib first
- ❌ **Don't over-engineer**: Start simple, evolve when needed
- ❌ **Don't write raw SQL**: Use repository pattern
- ❌ **Don't bypass event system**: Always emit events for state changes
- ❌ **Don't hardcode config values**: Use environment variables
- ❌ **Don't ignore errors**: Log and handle all exceptions
- ❌ **Don't block the main thread**: Use async for long-running operations

### When to Evolve

| Component | Current | Evolve To | When |
|-----------|---------|-----------|------|
| Event Bus | queue.Queue | Redis/RabbitMQ | Multi-process deployment needed |
| Sandbox | subprocess | Docker/K8s | Security isolation critical |
| A2A Protocol | In-process queues | HTTP/gRPC | Distributed deployment needed |
| State Machine | Enum-based | External library | >50 states or complex guards |
| Database | SQLite | MySQL/PostgreSQL | Concurrent writes or production |

---

## Testing Strategy

### Unit Tests

**State Machine** (`tests/test_state_machine.py`):
```python
def test_task_state_transitions():
    sm = StateMachine(TASK_TRANSITIONS)
    sm.initialize(TaskState.PENDING)
    assert sm.can_transition(TaskState.RUNNING)
    assert sm.transition(TaskState.RUNNING)
    assert sm.current_state == TaskState.RUNNING
    assert not sm.can_transition(TaskState.PENDING)  # Can't go back
```

**Event Bus** (`tests/test_event_bus.py`):
```python
def test_event_emit_subscribe():
    received = []
    def handler(event):
        received.append(event)
    
    event_bus.subscribe("test.event", handler)
    event_bus.emit("test.event", {"data": "test"})
    time.sleep(0.1)  # Wait for async processing
    assert len(received) == 1
```

**Watchdog** (`tests/test_watchdog.py`):
```python
def test_watchdog_timeout_detection():
    # Create task with short timeout
    task_id = "test-task"
    task_repo.create_task(task_id, "test", timeout_seconds=1)
    task_repo.update_task_state(task_id, TaskState.RUNNING)
    
    # Start watchdog
    watchdog.start()
    time.sleep(2)
    
    # Verify task marked as TIMEOUT
    task = task_repo.find_by_task_id(task_id)
    assert task.state == TaskState.TIMEOUT
```

### Integration Tests

**Agent Lifecycle**:
```python
def test_agent_lifecycle():
    # Register agent
    agent_id = agent_manager.register_agent("scheduling", "Test Agent")
    
    # Submit task
    task_id = agent_manager.submit_task("Test goal")
    
    # Wait for completion
    time.sleep(5)
    
    # Verify task completed
    task = task_repo.find_by_task_id(task_id)
    assert task.state == TaskState.SUCCESS
```

**Scenario Lifecycle**:
```python
def test_scenario_lifecycle():
    # Create scenario
    scenario_id = scenario_manager.create_scenario("simple_qa", "Test")
    
    # Start scenario
    scenario_manager.start_scenario(scenario_id)
    
    # Wait for completion
    time.sleep(10)
    
    # Verify scenario completed
    scenario = scenario_repo.find_by_scenario_id(scenario_id)
    assert scenario.state == ScenarioState.COMPLETED
```

### Manual Tests

**Gradio UI**:
1. Start platform: `python src/app.py --all`
2. Open browser: `http://localhost:8080`
3. Navigate to "Agent Registry" page
4. Register new agent (type: scheduling, name: "Test Agent")
5. Navigate to "Task Monitor" page
6. Verify agent appears in list

**Flask API**:
```bash
# Create task
curl -X POST http://localhost:5000/api/task \
  -H "Content-Type: application/json" \
  -d '{"goal": "Test task", "priority": 1}'

# Get task status
curl http://localhost:5000/api/task/<task_id>

# List events
curl http://localhost:5000/api/event?limit=10
```

---

## Deployment Strategy

### Development

**Environment**:
- SQLite (file-based, no setup)
- Single-process (Gradio + Flask in same process)
- Subprocess sandbox (simple, fast)
- Local filesystem (no external services)

**Startup**:
```bash
# Install dependencies
pip install -r requirements.txt

# Set environment
export DB_ENGINE=sqlite
export DB_NAME=agent_server.db
export GRADIO_PORT=8080
export FLASK_PORT=5000

# Start platform
python src/app.py --all
```

**Access**:
- Gradio UI: `http://localhost:8080`
- Flask API: `http://localhost:5000`
- Swagger docs: `http://localhost:5000/docs`

### Production

**Environment**:
- MySQL/PostgreSQL (connection pool)
- Multi-process (Gunicorn workers)
- Docker sandbox (strong isolation)
- Redis/RabbitMQ (event bus)
- gRPC (A2A protocol)
- Nginx reverse proxy

**Startup**:
```bash
# Set environment
export DB_ENGINE=mysql
export DB_NAME=agent_server
export DB_USER=agent_user
export DB_PASSWORD=***
export DB_HOST=localhost
export DB_PORT=3306

# Start Gradio (Gunicorn)
gunicorn -w 4 -b 0.0.0.0:8080 "src.app:create_app()"

# Start Flask (Gunicorn)
gunicorn -w 4 -b 0.0.0.0:5000 "src.api.flask_app:create_app()"

# Start Redis (for event bus)
redis-server
```

**Docker Compose** (optional):
```yaml
version: '3.8'
services:
  gradio:
    build: .
    ports:
      - "8080:8080"
    environment:
      - DB_ENGINE=mysql
      - DB_HOST=mysql
    depends_on:
      - mysql
      - redis
  
  flask:
    build: .
    ports:
      - "5000:5000"
    environment:
      - DB_ENGINE=mysql
      - DB_HOST=mysql
    depends_on:
      - mysql
      - redis
  
  mysql:
    image: mysql:8.0
    environment:
      - MYSQL_ROOT_PASSWORD=***
      - MYSQL_DATABASE=agent_server
    volumes:
      - mysql_data:/var/lib/mysql
  
  redis:
    image: redis:7
    ports:
      - "6379:6379"

volumes:
  mysql_data:
```

---

## Troubleshooting

### Common Issues

**Issue**: Task stuck in RUNNING state  
**Solution**: Check watchdog logs, verify `timeout_seconds` is set correctly

**Issue**: Event queue full  
**Solution**: Increase queue `maxsize` in `event_bus.py`, or add more event consumers

**Issue**: Sandbox timeout  
**Solution**: Increase `SANDBOX_TIMEOUT` env var, or optimize task script

**Issue**: Agent not responding  
**Solution**: Check agent logs, verify agent is registered with `agent_manager`

**Issue**: Database locked (SQLite)  
**Solution**: SQLite doesn't support concurrent writes well. Switch to MySQL for production, or ensure only one process writes to DB.

**Issue**: Gradio page not loading  
**Solution**: Check browser console for errors, verify page module exists in `pages/` directory

**Issue**: API returns 404  
**Solution**: Verify route is registered in `api/route/__init__.py`, check endpoint path

**Issue**: Events not persisting to DB  
**Solution**: Check `EventRepository` initialization, verify DB connection is established

### Debugging Tips

1. **Enable debug logging**: Set `LOG_LEVEL=DEBUG` in `.env`
2. **Check event log**: Use Event Log page or `GET /api/event` to see recent events
3. **Inspect database**: Use SQLite browser or `sqlite3` command to inspect DB
4. **Test API with curl**: Use curl commands to test API endpoints
5. **Check Gradio logs**: Gradio prints errors to console, check for stack traces

### Performance Tips

1. **Database indexing**: Add indexes for frequently queried fields
2. **Event queue size**: Increase `maxsize` if events are being dropped
3. **Watchdog interval**: Adjust `WATCHDOG_CHECK_INTERVAL` based on task duration
4. **Gradio auto-refresh**: Reduce timer interval for real-time updates (but increases load)
5. **Flask workers**: Increase Gunicorn workers for production (`-w 4` or more)

---

## Evolution Roadmap

### Phase 1 (Month 1): Foundation

- ✅ SQLite database
- ✅ Queue-based event bus
- ✅ Subprocess sandbox
- ✅ In-process A2A
- ✅ Basic Gradio UI
- ✅ Flask REST API

### Phase 2 (Month 2-3): Scaling

- ⏳ MySQL/PostgreSQL migration
- ⏳ Redis event bus (multi-process)
- ⏳ Docker sandbox (strong isolation)
- ⏳ HTTP A2A (multi-process agents)
- ⏳ Production deployment (Gunicorn, Nginx)

### Phase 3 (Month 4+): Distribution

- ⏳ Kubernetes deployment
- ⏳ gRPC A2A (cross-cluster)
- ⏳ RabbitMQ/Kafka (distributed events)
- ⏳ Prometheus + Grafana monitoring
- ⏳ Distributed tracing (Jaeger)

---

## Contributing

When adding new features:

1. **Follow existing patterns**: Use BaseModel, BaseRepository, EventBus
2. **Add tests**: Write unit tests for new components
3. **Update documentation**: Add to this README if it's a new pattern
4. **Emit events**: All state changes should emit events
5. **Handle errors**: Never crash the main loop, log and recover
6. **Keep it simple**: Ponytail mode - no over-engineering

---

**Last Updated**: 2026/07/20  
**Version**: 1.0.0  
**Maintainer**: Agent Server Platform Team

---

## Phase 2: Agent System Patterns

### Agent Architecture

**BaseAgent Interface**:
```python
class BaseAgent(ABC):
    def initialize(self) -> None:
        """Initialize agent resources"""
        
    def run(self, task_id: str, context: dict) -> dict:
        """Execute task and return result"""
        
    def cleanup(self) -> None:
        """Cleanup agent resources"""
```

**Agent Types**:
- `SchedulingAgent`: Decomposes goals into tasks, coordinates execution
- `ExecutionAgent`: Executes tasks in sandbox, handles code execution

**AgentRun**:
- Manages execution state (PENDING → RUNNING → COMPLETED/FAILED)
- Tracks result, error, started_at, completed_at
- Provides streaming output via SystemMessageOutput

### Sandbox System

**Sandbox Isolation**:
```python
sandbox = Sandbox(timeout=300, max_output_size=1048576)
sandbox.initialize()

# Execute Python code
result = sandbox.execute_python_code("print('Hello')")

# Execute shell command
result = sandbox.execute_shell_command("echo 'test'")

sandbox.cleanup()
```

**Features**:
- Isolated execution environment (temp directory)
- Timeout protection (prevents infinite loops)
- Output size limits (prevents memory issues)
- Automatic cleanup

**Evolution Path**:
- Phase 1: subprocess (current) - simple, fast
- Phase 2: Docker containers - strong isolation, resource limits
- Phase 3: Kubernetes pods - distributed, scalable

### A2A Protocol

**Queue-Based Messaging**:
```python
from core.a2a_protocol import a2a_protocol, A2AMessage

# Register agent
a2a_protocol.register_agent("agent-1")

# Send message
message = A2AMessage(
    from_agent="agent-1",
    to_agent="agent-2",
    message_type="request",
    payload={"data": "test"}
)
a2a_protocol.send(message)

# Receive message
received = a2a_protocol.receive("agent-2", timeout=10)
```

**Features**:
- In-process queue-based messaging
- Send/receive/broadcast operations
- Thread-safe with locking
- Evolution path to HTTP/gRPC

### Agent Manager

**Lifecycle Management**:
```python
from agents.agent_manager import agent_manager

# Register agent
agent_id = agent_manager.register_agent(
    agent_type="scheduling",
    name="My Agent",
    description="Test agent"
)

# Submit task
task_id = agent_manager.submit_task(
    goal="Test goal",
    agent_type="scheduling",
    timeout_seconds=60
)

# Get task status
status = agent_manager.get_task_status(task_id)
```

**Features**:
- Agent registration and tracking
- Task submission and execution
- State machine integration
- Event emission for all state changes

---

## Phase 3: Scenario System Patterns

### Scenario Architecture

**BaseScenario Interface**:
```python
class BaseScenario(ABC):
    def initialize(self, config: dict) -> None:
        """Initialize scenario with config"""
        
    def run(self) -> dict:
        """Execute scenario logic"""
        
    def cleanup(self) -> None:
        """Cleanup scenario resources"""
```

**Scenario Types**:
- `SimpleQAScenario`: Question-answering workflow
- `CodeExecutionScenario`: Code execution in sandbox

**ScenarioContext**:
- Manages scenario execution context
- Tracks state (INITIALIZING → RUNNING → COMPLETED/FAILED)
- Stores result and error information

### Scenario Manager

**Lifecycle Management**:
```python
from scenarios.scenario_manager import scenario_manager

# Create scenario
scenario_id = scenario_manager.create_scenario(
    scenario_type="simple_qa",
    name="QA Scenario",
    config={"question": "What is 2+2?"}
)

# Start scenario
scenario = SimpleQAScenario()
scenario_manager.start_scenario(scenario_id, scenario)

# Get status
status = scenario_manager.get_scenario_status(scenario_id)
```

**Features**:
- Scenario creation and registration
- State machine-driven lifecycle
- Background thread execution
- Integration with agent system

### Example Scenarios

**SimpleQAScenario**:
```python
config = {
    "question": "What is the capital of France?"
}
scenario = SimpleQAScenario()
result = scenario.start(config)
# Returns: {"success": True, "answer": "Paris"}
```

**CodeExecutionScenario**:
```python
config = {
    "code": "print('Hello from sandbox')"
}
scenario = CodeExecutionScenario()
result = scenario.start(config)
# Returns: {"success": True, "output": "Hello from sandbox\n"}
```

---

## Best Practices

### Error Handling

**Always handle errors gracefully**:
```python
try:
    result = agent.run(task_id, context)
    agent_run.complete(result)
except Exception as e:
    logger.error(f"Task failed: {e}")
    agent_run.fail(str(e))
```

**Never crash the agent loop**:
```python
# Bad: Let exception propagate
result = agent.run(task_id, context)

# Good: Catch and handle
try:
    result = agent.run(task_id, context)
except Exception as e:
    logger.error(f"Agent error: {e}")
    result = {"success": False, "error": str(e)}
```

### Event Emission

**Emit events for all state changes**:
```python
# Task lifecycle
event_bus.emit("task.created", {"task_id": task_id})
event_bus.emit("task.started", {"task_id": task_id})
event_bus.emit("task.completed", {"task_id": task_id, "result": result})
event_bus.emit("task.failed", {"task_id": task_id, "error": error})

# Scenario lifecycle
event_bus.emit("scenario.created", {"scenario_id": scenario_id})
event_bus.emit("scenario.started", {"scenario_id": scenario_id})
event_bus.emit("scenario.completed", {"scenario_id": scenario_id})
event_bus.emit("scenario.failed", {"scenario_id": scenario_id, "error": error})
```

### Resource Management

**Always cleanup resources**:
```python
sandbox = Sandbox()
try:
    sandbox.initialize()
    result = sandbox.execute_python_code(code)
finally:
    sandbox.cleanup()  # Always cleanup, even on error
```

**Use context managers**:
```python
with Sandbox() as sandbox:
    result = sandbox.execute_python_code(code)
# Automatic cleanup
```

### Concurrency

**Use thread-safe operations**:
```python
# Protect shared state with locks
with self.lock:
    self.agent_runs[task_id] = agent_run
```

**Use async for I/O operations**:
```python
async def execute_task(self, task_id, context):
    result = await self.sandbox.execute_async(code)
    return result
```

### Testing

**Test state machine transitions**:
```python
def test_state_transitions():
    sm = TASK_STATE_MACHINE
    sm.initialize(TaskState.PENDING)
    assert sm.can_transition(TaskState.RUNNING)
    sm.transition(TaskState.RUNNING)
    assert sm.current_state == TaskState.RUNNING
```

**Test event emission**:
```python
def test_events():
    received = []
    event_bus.subscribe("task.created", lambda e: received.append(e))
    event_bus.emit("task.created", {"task_id": "test"})
    time.sleep(0.1)
    assert len(received) == 1
```

---

## Troubleshooting

### Common Issues

**Task stuck in RUNNING state**:
- Check watchdog is running: `watchdog.start()`
- Verify task has timeout_seconds set
- Check agent is not deadlocked

**Event not received**:
- Verify event type matches subscription
- Check event bus is running
- Wait for async processing: `time.sleep(0.1)`

**Sandbox timeout**:
- Increase timeout: `Sandbox(timeout=600)`
- Check code for infinite loops
- Verify sandbox resources are cleaned up

**Database locked**:
- SQLite doesn't support concurrent writes well
- Use connection pooling
- Consider migrating to MySQL/PostgreSQL

**Agent not executing**:
- Check agent is registered: `agent_manager.list_agents()`
- Verify task is submitted: `task_repo.find_by_task_id(task_id)`
- Check logs for errors

---

## Evolution Roadmap

### Phase 4: Production Readiness (Future)

**MySQL/PostgreSQL Migration**:
```python
# Update .env
DB_ENGINE=mysql
DB_HOST=localhost
DB_PORT=3306
DB_NAME=agent_server
DB_USER=agent_user
DB_PASSWORD=***
```

**Docker Sandbox**:
```python
# Replace subprocess with Docker
class DockerSandbox(Sandbox):
    def execute_python_code(self, code):
        # Use docker SDK to run code in container
        container = docker_client.containers.run(
            "python:3.11",
            f"python -c '{code}'",
            detach=True
        )
        return container.wait()
```

**Redis Event Bus**:
```python
# Replace queue.Queue with Redis pub/sub
class RedisEventBus(EventBus):
    def emit(self, event_type, data):
        self.redis_client.publish(event_type, json.dumps(data))
    
    def subscribe(self, event_type, handler):
        pubsub = self.redis_client.pubsub()
        pubsub.subscribe(event_type)
        for message in pubsub.listen():
            handler(message)
```

**gRPC A2A Protocol**:
```python
# Replace in-process queues with gRPC
class GRPCA2AProtocol(A2AProtocol):
    def send(self, message):
        stub = A2AServiceStub(channel)
        stub.SendMessage(message)
```

---

## Summary

**Current Status**:
- ✅ Phase 1: Core Infrastructure (SQLite, Event Bus, Watchdog, UI)
- ✅ Phase 2: Agent System (Scheduling, Execution, Sandbox, A2A)
- ✅ Phase 3: Scenario System (Scenarios, Manager, Examples)
- ⏳ Phase 4: Production Readiness (Future)

**Key Principles**:
1. Start simple, evolve when needed
2. Use stdlib first, external deps only when justified
3. Always emit events for state changes
4. Handle errors gracefully, never crash
5. Test thoroughly (unit + integration)
6. Document decisions in knowledge base

**Next Steps**:
1. Production deployment with MySQL/PostgreSQL
2. Docker sandbox for strong isolation
3. Redis event bus for multi-process
4. gRPC A2A for distributed agents
5. Monitoring and alerting

---

**Document Version**: 2.0  
**Last Updated**: 2026/07/21  
**Status**: Phase 1-3 Complete, Ready for Phase 4
