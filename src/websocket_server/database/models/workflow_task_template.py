from datetime import datetime
from typing import Type
from .base import BaseModel


class WorkflowTaskTemplate(BaseModel):
    """Per-step template within a published workflow DAG."""
    __tablename__ = "workflow_task_templates"
    __primary_key__ = "id"
    __fields__ = {
        "id": int,
        "template_id": str,
        "workflow_id": str,
        "step_id": str,
        "goal_template": str,
        "depends_on": str,
        "agent_role": str,
        "system_prompt": str,
        "server_id": str,
        "timeout_seconds": int,
        "input_param_mapping": str,
        "experience_note": str,
        "step_order": int,
        "created_at": datetime,
        "updated_at": datetime,
    }
    __default_order__ = "step_order ASC"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
