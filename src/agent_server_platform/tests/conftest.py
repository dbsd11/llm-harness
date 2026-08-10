"""
Test configuration and shared fixtures.

Provides:
- sys.path setup for src/ imports
- Fresh SQLite database per test
- Singleton reset helpers for global objects
"""
import sys
import os
import uuid
import tempfile
import shutil

import pytest

# ── Path setup ──────────────────────────────────────────────────────────────
# Source code uses relative imports from src/ (e.g. "from database...")
# so we need src/ on sys.path.
SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _reset_connection_manager():
    """Reset the global ConnectionManager singleton.

    Must reset BOTH the module-level global AND the class-level __new__
    singleton to ensure a fresh instance is created next time.
    """
    from database.connection import reset_connection_manager, ConnectionManager
    reset_connection_manager()
    ConnectionManager._instance = None


def _init_database():
    """Create all tables in the current DB."""
    from database import init_database
    init_database()


def _close_database():
    """Close current DB connections."""
    from database import close_database
    close_database()


def _reset_event_bus_subscribers():
    """Clear all event bus subscribers to prevent cross-test leaks."""
    try:
        from core.event_bus import event_bus
        event_bus.subscribers.clear()
    except Exception:
        pass


def _reset_a2a_protocol():
    """Clear all registered agents from A2A protocol."""
    try:
        from core.a2a_protocol import a2a_protocol
        a2a_protocol.agent_queues.clear()
    except Exception:
        pass


def _reset_state_machines():
    """Reset global state machines to uninitialized state."""
    from core.state_machine import TASK_STATE_MACHINE, SCENARIO_STATE_MACHINE
    TASK_STATE_MACHINE.current_state = None
    SCENARIO_STATE_MACHINE.current_state = None


def _stop_mqs_workers():
    """No-op: the per-scenario in-process ExecutionWorker was removed.

    Dispatched tasks are now consumed by the CentralDispatcher (WS-server
    process), which isn't started in unit tests.
    """
    pass


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def fresh_database(tmp_path, monkeypatch):
    """
    Provide a fresh SQLite database for every test.

    - Sets DB_ENGINE=sqlite and DB_NAME to a temp file
    - Resets ConnectionManager singleton
    - Initializes all tables
    - Tears down connections after test
    - Resets global state (event bus subscribers, A2A queues, state machines)
    """
    db_file = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_ENGINE", "sqlite")
    monkeypatch.setenv("DB_NAME", db_file)

    _reset_connection_manager()
    _init_database()

    yield

    _stop_mqs_workers()
    _reset_event_bus_subscribers()
    _reset_a2a_protocol()
    _reset_state_machines()
    _close_database()
    _reset_connection_manager()

    # Clean up temp DB file
    if os.path.exists(db_file):
        try:
            os.remove(db_file)
        except OSError:
            pass


@pytest.fixture
def task_repo():
    """Provide a TaskRepository instance."""
    from database.repositories.task_repository import TaskRepository
    return TaskRepository()


@pytest.fixture
def agent_repo():
    """Provide an AgentRepository instance."""
    from database.repositories.agent_repository import AgentRepository
    return AgentRepository()


@pytest.fixture
def scenario_repo():
    """Provide a ScenarioRepository instance."""
    from database.repositories.scenario_repository import ScenarioRepository
    return ScenarioRepository()


@pytest.fixture
def event_repo():
    """Provide an EventRepository instance."""
    from database.repositories.event_repository import EventRepository
    return EventRepository()


@pytest.fixture
def event_bus():
    """Provide the global EventBus instance (already imported)."""
    from core.event_bus import event_bus
    return event_bus


@pytest.fixture
def a2a():
    """Provide a clean A2AProtocol instance."""
    from core.a2a_protocol import a2a_protocol
    return a2a_protocol


@pytest.fixture
def sandbox():
    """Provide a Sandbox instance, auto-cleaned."""
    from core.sandbox import Sandbox
    sb = Sandbox()
    sb.initialize()
    yield sb
    sb.cleanup()


@pytest.fixture
def unique_id():
    """Generate a unique ID for test isolation."""
    return str(uuid.uuid4())
