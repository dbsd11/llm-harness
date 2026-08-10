# Message model - persisted agent-to-agent communication
from datetime import datetime
from typing import Dict, Any, Type
from .base import BaseModel


class Message(BaseModel):
    """Message model - stores agent-to-agent messages for scenario execution tracking"""
    __tablename__ = "messages"
    __primary_key__ = "id"
    __fields__ = {
        "id": int,
        "scenario_id": str,
        "task_id": str,
        "from_agent": str,   # "scheduling" / "execution"
        "to_agent": str,     # "scheduling" / "execution"
        "message_type": str, # "dispatch" / "reply"
        "content": str,      # JSON string with full message payload
        "timestamp": datetime,
        "acked": int,        # dispatch only: 0=unacked, 1=acked (ack-after-execute)
    }
    __default_order__ = "timestamp DESC"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
