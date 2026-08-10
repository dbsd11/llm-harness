# HumanTask repository — CRUD + query for pending human-agent tasks
import json
from datetime import datetime
from typing import Optional, List
from .base_repository import BaseRepository
from ..models.local_human_task import HumanTask


class HumanTaskRepository(BaseRepository[HumanTask]):
    """Repository for the human_tasks table."""

    def __init__(self):
        super().__init__(HumanTask)

    def create_task(self, task_id: str, server_id: str, goal: str = "",
                    context: dict = None, parent_task_id: str = "") -> Optional[int]:
        """Insert a pending task. No-op if task_id already exists (idempotent)."""
        existing = self.find_by_task_id(task_id)
        if existing:
            return None  # already stored
        model = HumanTask(
            task_id=task_id,
            server_id=server_id,
            goal=goal or "",
            context=json.dumps(context, ensure_ascii=False) if context else "{}",
            parent_task_id=parent_task_id or "",
            arrived_at=datetime.now().isoformat(),
            status="pending",
        )
        return self.create(model)

    def find_pending_by_server_id(self, server_id: str) -> List[HumanTask]:
        """Return all pending tasks for a given human agent identity."""
        return self.find_by_criteria(
            {"server_id": server_id, "status": "pending"},
            order_by="id ASC",
        )

    def find_by_task_id(self, task_id: str) -> Optional[HumanTask]:
        """Find a human task by its upstream task_id."""
        results = self.find_by_criteria({"task_id": task_id}, limit=1)
        return results[0] if results else None

    def mark_submitted(self, task_id: str) -> bool:
        """Mark a human task as submitted."""
        ph = self.placeholder
        sql = f"UPDATE {self.table_name} SET status={ph} WHERE task_id={ph}"
        from database.local_connection import get_connection_manager
        cm = get_connection_manager()
        with cm.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, ("submitted", task_id))
            conn.commit()
            return cursor.rowcount > 0