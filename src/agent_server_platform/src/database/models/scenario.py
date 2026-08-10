# Scenario model
from datetime import datetime
from typing import Dict, Any, Type
from .base import BaseModel


class Scenario(BaseModel):
    """Scenario model - represents scenarios in the scenario state machine"""
    __tablename__ = "scenarios"
    __primary_key__ = "id"
    __fields__ = {
        "id": int,
        "scenario_id": str,
        "scenario_type": str,  # Scenario class name
        "name": str,
        "description": str,
        "state": str,  # ScenarioState enum: initializing, running, completed, failed, cancelled
        "config": str,  # JSON string
        "context": str,  # JSON string (scenario-specific context)
        "created_by": int,  # User ID
        "created_at": datetime,
        "updated_at": datetime,
        "started_at": datetime,
        "completed_at": datetime,
    }
    __default_order__ = "created_at DESC"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
