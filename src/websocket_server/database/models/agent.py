# Agent model
from datetime import datetime
from typing import Dict, Any, Type
from .base import BaseModel


class Agent(BaseModel):
    """Agent model - represents registered agents in the system"""
    __tablename__ = "agents"
    __primary_key__ = "id"
    __fields__ = {
        "id": int,
        "agent_id": str,
        "scenario_id": str,  # Links agent to scenario (cleanup when scenario ends)
        "agent_type": str,  # 'scheduling', 'execution'
        "name": str,
        "description": str,
        "config": str,  # JSON string
        "status": str,  # 'active', 'inactive', 'error'
        "created_at": datetime,
        "updated_at": datetime,
    }
    __default_order__ = "id DESC"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
