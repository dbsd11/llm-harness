# Message Queue - DB-backed message transport.
#
# Producers write dispatch rows; the CentralDispatcher (see
# core/central_dispatcher.py) consumes them globally and routes each to a
# remote execution-agent-server (over WebSocket) or to local execution.
# Replies are written back as rows by the dispatcher and collected here.
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from datetime import datetime
import json

from logger import logger
from database.repositories.message_repository import MessageRepository
from database.repositories.consumer_offset_repository import ConsumerOffsetRepository
from database.models.message import Message


@dataclass
class TaskMessage:
    """Task dispatched from SchedulingAgent to ExecutionAgent"""
    task_id: str
    parent_task_id: str
    goal: str
    context: dict = field(default_factory=dict)


@dataclass
class ReplyMessage:
    """Result from ExecutionAgent back to SchedulingAgent"""
    task_id: str
    success: bool
    result: dict = field(default_factory=dict)
    pending_review: bool = False  # True when task is gated for human acceptance


def _consumer_id_scheduling(scenario_id: str) -> str:
    return f"scheduling_agent:{scenario_id}"


class MessageQueueService:
    """
    DB-backed message queue service.

    Writers put dispatch rows; the CentralDispatcher consumes them. Replies are
    read here by collect_replies.
    """

    def dispatch_subtasks(self, scenario_id: str,
                          subtasks: List[TaskMessage],
                          max_workers: int = 3) -> Dict[str, bool]:
        """Write dispatch messages to DB with retry and status tracking.

        The CentralDispatcher's global consumer picks them up and routes each
        (WS forward or local). max_workers is kept in the signature for
        call-site compatibility but no longer starts a worker here.

        Returns:
            Dict mapping task_id to success status (True if dispatch message created)
        """
        msg_repo = MessageRepository()
        dispatch_status = {}

        for msg in subtasks:
            max_retries = 3
            success = False

            for attempt in range(max_retries):
                try:
                    msg_repo.create(Message(
                        scenario_id=scenario_id,
                        task_id=msg.task_id,
                        from_agent="scheduling",
                        to_agent="execution",
                        message_type="dispatch",
                        content=json.dumps({
                            "task_id": msg.task_id,
                            "parent_task_id": msg.parent_task_id,
                            "goal": msg.goal,
                            "context": msg.context,
                        }, ensure_ascii=False),
                        timestamp=datetime.now(),
                        acked=0,
                    ))
                    success = True
                    logger.info(f"Dispatch message created for task {msg.task_id} "
                               f"(attempt {attempt + 1})")
                    break
                except Exception as e:
                    logger.error(f"Failed to persist dispatch message for task {msg.task_id} "
                               f"(attempt {attempt + 1}/{max_retries}): {e}")
                    if attempt < max_retries - 1:
                        import time
                        time.sleep(0.1 * (attempt + 1))  # Exponential backoff

            dispatch_status[msg.task_id] = success
            if not success:
                logger.error(f"Failed to create dispatch message for task {msg.task_id} "
                           f"after {max_retries} attempts")

        success_count = sum(1 for s in dispatch_status.values() if s)
        logger.info(f"Dispatched {success_count}/{len(subtasks)} subtask(s) to scenario {scenario_id}")

        return dispatch_status

    def collect_replies(self, scenario_id: str, expected_count: int,
                        timeout: int = 300,
                        expected_task_ids: List[str] = None) -> List[ReplyMessage]:
        """Poll DB for reply messages until expected_count reached or timeout.

        Args:
            scenario_id: Scenario ID
            expected_count: Number of replies to collect
            timeout: Timeout in seconds
            expected_task_ids: Optional list of task IDs we're waiting for.
                             Used for better logging and timeout tracking.
        """
        replies: List[ReplyMessage] = []
        replied_task_ids: set = set()
        deadline = datetime.now().timestamp() + timeout
        consumer_id = _consumer_id_scheduling(scenario_id)
        msg_repo = MessageRepository()
        offset_repo = ConsumerOffsetRepository()

        expected_set = set(expected_task_ids) if expected_task_ids else None

        import time
        last_log_time = time.time()
        while len(replies) < expected_count:
            remaining = deadline - datetime.now().timestamp()
            if remaining <= 0:
                missing = expected_set - replied_task_ids if expected_set else set()
                logger.warning(
                    f"collect_replies timeout: got {len(replies)}/{expected_count} "
                    f"for scenario {scenario_id}"
                    + (f", missing replies from: {missing}" if missing else "")
                )
                break

            pending = msg_repo.find_pending_messages(
                consumer_id, scenario_id, "reply", limit=expected_count - len(replies)
            )

            for msg_record in pending:
                try:
                    content = json.loads(msg_record.content) if msg_record.content else {}
                    reply = ReplyMessage(
                        task_id=content.get("task_id", msg_record.task_id),
                        success=content.get("success", False),
                        result=content.get("result", {}),
                        pending_review=content.get("pending_review", False),
                    )
                    # Deduplicate: skip if we already collected a reply for
                    # this task_id (e.g. stale result from a superseded
                    # execution after retry).
                    if reply.task_id in replied_task_ids:
                        offset_repo.update_offset(consumer_id, msg_record.id)
                        continue
                    replies.append(reply)
                    replied_task_ids.add(reply.task_id)
                    offset_repo.update_offset(consumer_id, msg_record.id)
                except (json.JSONDecodeError, TypeError):
                    logger.error(f"Failed to parse reply message {msg_record.id}")
                    offset_repo.update_offset(consumer_id, msg_record.id)

            if len(replies) < expected_count:
                # Log waiting status periodically (every 30 seconds)
                current_time = time.time()
                if expected_set and current_time - last_log_time > 30:
                    still_waiting = expected_set - replied_task_ids
                    logger.info(
                        f"collect_replies waiting for {len(still_waiting)} tasks: "
                        f"{still_waiting} (elapsed: {current_time - (deadline - timeout):.1f}s)"
                    )
                    last_log_time = current_time

                time.sleep(min(0.5, remaining))

        return replies


# Global singleton
mqs = MessageQueueService()
