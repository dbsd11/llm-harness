# Event model
from datetime import datetime
from typing import Dict, Any, Type
from .base import BaseModel


class Event(BaseModel):
    """Event model - represents system events for instrumentation and tracking (埋点)"""
    __tablename__ = "events"
    __primary_key__ = "id"
    __fields__ = {
        "id": int,
        "event_type": str,
        "data": str,        # JSON string
        "trace_id": str,    # Links related events across a flow
        "metadata": str,    # JSON string with extra context
        "timestamp": datetime,
    }
    __default_order__ = "timestamp DESC"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
