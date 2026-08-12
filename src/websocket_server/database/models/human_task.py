# HumanTask model — tracks tasks dispatched to human agents via WS event bridge
from datetime import datetime
from typing import Dict, Type
from .base import BaseModel


class HumanTask(BaseModel):
    """A task dispatched to a human agent, pending manual submission.

    Written by ws_event_subscriber when the remote WS server emits a
    task_dispatched event. Read by the human_tasks Gradio page timer.
    """
    __tablename__ = "human_tasks"
    __primary_key__ = "id"
    __fields__ = {
        "id": int,
        "task_id": str,        # upstream task ID from remote WS server
        "server_id": str,      # target human agent identity
        "goal": str,
        "context": str,        # JSON string
        "parent_task_id": str,
        "arrived_at": str,     # ISO timestamp
        "status": str,         # 'pending' | 'submitted'
        "tenant_id": str,      # 租户 ID（来自 API Key）
    }
    __default_order__ = "id ASC"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)