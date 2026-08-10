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
                          max_workers: int = 3) -> None:
        """Write dispatch messages to DB.

        The CentralDispatcher's global consumer picks them up and routes each
        (WS forward or local). max_workers is kept in the signature for
        call-site compatibility but no longer starts a worker here.
        """
        msg_repo = MessageRepository()
        for msg in subtasks:
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
            except Exception as e:
                logger.error(f"Failed to persist dispatch message: {e}")
        logger.info(f"Dispatched {len(subtasks)} subtask(s) to scenario {scenario_id} "
                    f"with {max_workers} parallel workers")

    def collect_replies(self, scenario_id: str, expected_count: int,
                        timeout: int = 300) -> List[ReplyMessage]:
        """Poll DB for reply messages until expected_count reached or timeout"""
        replies: List[ReplyMessage] = []
        deadline = datetime.now().timestamp() + timeout
        consumer_id = _consumer_id_scheduling(scenario_id)
        msg_repo = MessageRepository()
        offset_repo = ConsumerOffsetRepository()

        import time
        while len(replies) < expected_count:
            remaining = deadline - datetime.now().timestamp()
            if remaining <= 0:
                logger.warning(
                    f"collect_replies timeout: got {len(replies)}/{expected_count} "
                    f"for scenario {scenario_id}"
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
                    replies.append(reply)
                    offset_repo.update_offset(consumer_id, msg_record.id)
                except (json.JSONDecodeError, TypeError):
                    logger.error(f"Failed to parse reply message {msg_record.id}")
                    offset_repo.update_offset(consumer_id, msg_record.id)

            if len(replies) < expected_count:
                time.sleep(min(0.5, remaining))

        return replies


# Global singleton
mqs = MessageQueueService()
