from datetime import datetime
from typing import Type
from .base import BaseModel


class WorkflowExecution(BaseModel):
    """Tracks a single execution run of a published workflow."""
    __tablename__ = "workflow_executions"
    __primary_key__ = "id"
    __fields__ = {
        "id": int,
        "execution_id": str,
        "workflow_id": str,
        "workflow_version": int,
        "state": str,
        "input_params": str,
        "task_results": str,
        "total_steps": int,
        "completed_steps": int,
        "failed_steps": int,
        "error": str,
        "scenario_id": str,
        "parent_task_id": str,
        "started_at": datetime,
        "completed_at": datetime,
        "created_at": datetime,
        "updated_at": datetime,
    }
    __default_order__ = "created_at DESC"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
