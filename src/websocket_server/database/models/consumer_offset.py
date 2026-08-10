# ConsumerOffset model - tracks per-BOT message consumption position
from datetime import datetime
from typing import Dict, Any, Type
from .base import BaseModel


class ConsumerOffset(BaseModel):
    """Consumer offset model - each BOT maintains its own read position"""
    __tablename__ = "consumer_offsets"
    __primary_key__ = "consumer_id"
    __fields__ = {
        "consumer_id": str,   # e.g. "execution_worker:{scenario_id}"
        "last_message_id": int,
        "updated_at": datetime,
    }
    __default_order__ = ""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
