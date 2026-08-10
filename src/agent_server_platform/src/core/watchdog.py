# Watchdog - timeout detection and recovery
import threading
import time
from datetime import datetime
from typing import Optional

from database.repositories.task_repository import TaskRepository
from core.state_machine import TaskState
from core.event_bus import event_bus
from logger import logger


class Watchdog:
    """
    Timeout detection and recovery system.

    Responsibilities:
    - Monitor running tasks for timeout
    - Attempt recovery (retry/cancel)
    - Emit watchdog events
    """

    def __init__(self, check_interval: int = 10):
        """
        Initialize watchdog

        Args:
            check_interval: How often to check for timeouts (seconds)
        """
        self.check_interval = check_interval
        self.task_repo = TaskRepository()
        self.running = False
        self.worker_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start watchdog monitoring"""
        if self.running:
            return

        self.running = True
        self.worker_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.worker_thread.start()
        logger.info(f"Watchdog started (check interval: {self.check_interval}s)")

    def stop(self) -> None:
        """Stop watchdog monitoring"""
        self.running = False
        if self.worker_thread:
            self.worker_thread.join(timeout=5)
        logger.info("Watchdog stopped")

    def _monitor_loop(self) -> None:
        """Main monitoring loop"""
        while self.running:
            try:
                self._check_timeouts()
                time.sleep(self.check_interval)
            except Exception as e:
                logger.error(f"Watchdog error: {e}")

    def _check_timeouts(self) -> None:
        """Check for timed-out tasks"""
        # Query tasks in RUNNING state
        running_tasks = self.task_repo.find_by_state(TaskState.RUNNING.value)

        now = datetime.now()
        for task in running_tasks:
            if not task.started_at:
                continue

            # Parse started_at timestamp
            if isinstance(task.started_at, str):
                try:
                    started_at = datetime.fromisoformat(task.started_at)
                except:
                    continue
            else:
                started_at = task.started_at

            timeout_seconds = task.timeout_seconds or 3600  # Default 1h

            elapsed = (now - started_at).total_seconds()
            if elapsed > timeout_seconds:
                logger.warning(f"Task {task.task_id} timed out after {elapsed:.1f}s")
                event_bus.emit("watchdog.timeout_detected", {
                    "task_id": task.task_id,
                    "elapsed_seconds": elapsed,
                })

                # Attempt recovery
                self._attempt_recovery(task.task_id, task)

    def _attempt_recovery(self, task_id: str, task) -> None:
        """
        Attempt to recover timed-out task

        Args:
            task_id: Task ID
            task: Task object
        """
        retry_count = task.retry_count or 0
        max_retries = task.max_retries or 3

        if retry_count < max_retries:
            # Retry: transition to PENDING, increment retry_count
            logger.info(f"Retrying task {task_id} (attempt {retry_count + 1}/{max_retries})")
            self.task_repo.update_task_state(task_id, TaskState.PENDING.value)
            self.task_repo.increment_retry_count(task_id)
            event_bus.emit("watchdog.recovery_attempted", {
                "task_id": task_id,
                "action": "retry",
                "retry_count": retry_count + 1,
            })
        else:
            # Give up: transition to TIMEOUT
            logger.error(f"Task {task_id} exceeded max retries, marking as TIMEOUT")
            self.task_repo.update_task_state(task_id, TaskState.TIMEOUT.value)
            event_bus.emit("watchdog.recovery_failed", {
                "task_id": task_id,
                "reason": "max_retries_exceeded",
            })


# Global watchdog instance
watchdog = Watchdog()
