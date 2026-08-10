"""
Unit / integration tests for core/watchdog.py

Tests:
- Watchdog initialization
- Start/stop lifecycle
- Timeout detection for RUNNING tasks
- Recovery with retries (PENDING + increment retry_count)
- Max retries exceeded (TIMEOUT)
- Event emission during recovery
"""
import pytest
import time
import uuid
from datetime import datetime, timedelta

from core.watchdog import Watchdog
from core.state_machine import TaskState
from database.repositories.task_repository import TaskRepository
from database.models.task import Task


def _create_timed_out_task(task_repo, task_id, retry_count=0, max_retries=3,
                            timeout_seconds=3600, hours_ago=2):
    """Helper: create a RUNNING task that has timed out."""
    task = Task(
        task_id=task_id,
        goal="Timeout test task",
        state=TaskState.RUNNING.value,
        priority=1,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        retry_count=retry_count,
        created_at=datetime.now() - timedelta(hours=hours_ago),
        started_at=datetime.now() - timedelta(hours=hours_ago),
        updated_at=datetime.now() - timedelta(hours=hours_ago),
    )
    task_repo.create(task)
    return task


class TestWatchdogInit:
    def test_default_check_interval(self):
        wd = Watchdog()
        assert wd.check_interval == 10

    def test_custom_check_interval(self):
        wd = Watchdog(check_interval=5)
        assert wd.check_interval == 5

    def test_initial_running_state(self):
        wd = Watchdog()
        assert wd.running is False


class TestWatchdogStartStop:
    def test_start_sets_running(self):
        wd = Watchdog(check_interval=100)
        wd.start()
        assert wd.running is True
        wd.stop()

    def test_stop_clears_running(self):
        wd = Watchdog(check_interval=100)
        wd.start()
        wd.stop()
        assert wd.running is False

    def test_start_idempotent(self):
        wd = Watchdog(check_interval=100)
        wd.start()
        wd.start()  # should not create another thread
        assert wd.running is True
        wd.stop()

    def test_stop_without_start(self):
        wd = Watchdog()
        wd.stop()  # should not raise


class TestWatchdogTimeoutDetection:
    def test_detects_timed_out_task_and_retries(self, task_repo):
        """Watchdog retries RUNNING tasks that have exceeded their timeout."""
        task_id = f"wd-retry-{uuid.uuid4().hex[:8]}"
        _create_timed_out_task(task_repo, task_id, retry_count=0, max_retries=3)

        wd = Watchdog(check_interval=1)
        wd.start()
        time.sleep(3)
        wd.stop()

        task = task_repo.find_by_task_id(task_id)
        assert task is not None
        # retry_count=0 < max_retries=3 → should retry (PENDING + increment)
        assert task.state == TaskState.PENDING.value
        assert task.retry_count == 1

    def test_retry_increments_count(self, task_repo):
        task_id = f"wd-retry2-{uuid.uuid4().hex[:8]}"
        _create_timed_out_task(task_repo, task_id, retry_count=1, max_retries=3)

        wd = Watchdog(check_interval=1)
        wd.start()
        time.sleep(3)
        wd.stop()

        task = task_repo.find_by_task_id(task_id)
        assert task.state == TaskState.PENDING.value
        assert task.retry_count == 2

    def test_max_retries_exceeded_marks_timeout(self, task_repo):
        task_id = f"wd-timeout-{uuid.uuid4().hex[:8]}"
        _create_timed_out_task(task_repo, task_id, retry_count=3, max_retries=3)

        wd = Watchdog(check_interval=1)
        wd.start()
        time.sleep(3)
        wd.stop()

        task = task_repo.find_by_task_id(task_id)
        assert task is not None
        # retry_count=3 < max_retries=3 is False → TIMEOUT
        assert task.state == TaskState.TIMEOUT.value

    def test_does_not_touch_non_running_tasks(self, task_repo):
        task_id = f"wd-pending-{uuid.uuid4().hex[:8]}"
        task = Task(
            task_id=task_id,
            goal="Pending task",
            state=TaskState.PENDING.value,
            priority=0,
            timeout_seconds=1,
            max_retries=3,
            retry_count=0,
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        task_repo.create(task)

        wd = Watchdog(check_interval=1)
        wd.start()
        time.sleep(2)
        wd.stop()

        task = task_repo.find_by_task_id(task_id)
        assert task.state == TaskState.PENDING.value  # unchanged

    def test_does_not_touch_non_timed_out_task(self, task_repo):
        task_id = f"wd-fresh-{uuid.uuid4().hex[:8]}"
        task = Task(
            task_id=task_id,
            goal="Fresh running task",
            state=TaskState.RUNNING.value,
            priority=0,
            timeout_seconds=3600,  # 1 hour timeout
            max_retries=3,
            retry_count=0,
            created_at=datetime.now(),
            started_at=datetime.now(),  # just started
            updated_at=datetime.now(),
        )
        task_repo.create(task)

        wd = Watchdog(check_interval=1)
        wd.start()
        time.sleep(2)
        wd.stop()

        task = task_repo.find_by_task_id(task_id)
        assert task.state == TaskState.RUNNING.value  # unchanged


class TestWatchdogEvents:
    def test_emits_timeout_detected_event(self, event_bus, task_repo):
        received = []
        handler = lambda e: received.append(e)
        event_bus.subscribe("watchdog.timeout_detected", handler)

        task_id = f"wd-evt-{uuid.uuid4().hex[:8]}"
        _create_timed_out_task(task_repo, task_id, retry_count=0, max_retries=3)

        wd = Watchdog(check_interval=1)
        wd.start()
        time.sleep(3)
        wd.stop()

        event_bus.unsubscribe("watchdog.timeout_detected", handler)
        timeout_events = [e for e in received
                          if e["event_type"] == "watchdog.timeout_detected"]
        assert len(timeout_events) >= 1

    def test_emits_recovery_attempted_event(self, event_bus, task_repo):
        received = []
        handler = lambda e: received.append(e)
        event_bus.subscribe("watchdog.recovery_attempted", handler)

        task_id = f"wd-rec-{uuid.uuid4().hex[:8]}"
        _create_timed_out_task(task_repo, task_id, retry_count=0, max_retries=3)

        wd = Watchdog(check_interval=1)
        wd.start()
        time.sleep(3)
        wd.stop()

        event_bus.unsubscribe("watchdog.recovery_attempted", handler)
        recovery_events = [e for e in received
                           if e["event_type"] == "watchdog.recovery_attempted"]
        assert len(recovery_events) >= 1

    def test_emits_recovery_failed_when_max_exceeded(self, event_bus, task_repo):
        received = []
        handler = lambda e: received.append(e)
        event_bus.subscribe("watchdog.recovery_failed", handler)

        task_id = f"wd-fail-{uuid.uuid4().hex[:8]}"
        _create_timed_out_task(task_repo, task_id, retry_count=3, max_retries=3)

        wd = Watchdog(check_interval=1)
        wd.start()
        time.sleep(3)
        wd.stop()

        event_bus.unsubscribe("watchdog.recovery_failed", handler)
        failed_events = [e for e in received
                         if e["event_type"] == "watchdog.recovery_failed"]
        assert len(failed_events) >= 1
