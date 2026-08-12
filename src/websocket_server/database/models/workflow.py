from datetime import datetime
from typing import Type
from .base import BaseModel


class Workflow(BaseModel):
    """Published workflow definition - a reusable DAG extracted from a completed scenario."""
    __tablename__ = "workflows"
    __primary_key__ = "id"
    __fields__ = {
        "id": int,
        "workflow_id": str,
        "name": str,
        "description": str,
        "source_scenario_id": str,
        "dag_definition": str,
        "input_schema": str,
        "agent_roles": str,
        "experience_context": str,
        "version": int,
        "state": str,
        "created_by": str,
        "created_at": datetime,
        "updated_at": datetime,
        "tenant_id": str,  # 租户 ID（来自 API Key）
    }
    __default_order__ = "created_at DESC"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
