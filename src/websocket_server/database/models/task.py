# Task model
from datetime import datetime
from typing import Dict, Any, Type
from .base import BaseModel


class Task(BaseModel):
    """Task model - represents tasks in the task state machine"""
    __tablename__ = "tasks"
    __primary_key__ = "id"
    __fields__ = {
        "id": int,
        "task_id": str,
        "agent_run_id": str,
        "scenario_id": str,
        "parent_task_id": str,  # For task hierarchies
        "topic_id": str,        # Groups subtasks from same decomposition (子主题)
        "idempotency_key": str, # Prevents duplicate task creation (幂等控制)
        "depends_on": str,    # JSON array of predecessor task_ids (DAG deps)
        "goal": str,
        "state": str,  # TaskState enum: pending, running, waiting, success, failed, timeout, cancelled
        "priority": int,
        "timeout_seconds": int,
        "max_retries": int,
        "retry_count": int,
        "context": str,  # JSON string
        "result": str,  # JSON string
        "error": str,
        "agent_name": str,  # Agent name (captured at execution time)
        "agent_role": str,  # Agent role (captured at execution time)
        "execution_duration": float,  # Execution duration in seconds
        "review_feedback": str,  # Human supplementary feedback on rejection (manual_acceptance)
        "created_at": datetime,
        "updated_at": datetime,
        "started_at": datetime,
        "completed_at": datetime,
        "tenant_id": str,  # 租户 ID（来自 API Key）
    }
    __default_order__ = "created_at DESC"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
