from typing import Optional, List
from datetime import datetime
from .base_repository import BaseRepository
from ..models.workflow import Workflow


class WorkflowRepository(BaseRepository[Workflow]):

    def __init__(self):
        super().__init__(Workflow)

    def find_by_workflow_id(self, workflow_id: str) -> Optional[Workflow]:
        results = self.find_by_criteria({"workflow_id": workflow_id})
        return results[0] if results else None

    def find_by_state(self, state: str, limit: int = 100,
                      tenant_id: str = None) -> List[Workflow]:
        if tenant_id:
            return self._tenant_query({"state": state}, tenant_id, limit=limit)
        return self.find_by_criteria({"state": state}, limit=limit)

    def find_by_source_scenario_id(self, scenario_id: str) -> List[Workflow]:
        return self.find_by_criteria({"source_scenario_id": scenario_id})

    def mark_as_active(self, workflow_id: str) -> bool:
        wf = self.find_by_workflow_id(workflow_id)
        if not wf:
            return False
        wf.state = "active"
        wf.updated_at = datetime.now()
        return self.update(wf)

    def mark_as_archived(self, workflow_id: str) -> bool:
        wf = self.find_by_workflow_id(workflow_id)
        if not wf:
            return False
        wf.state = "archived"
        wf.updated_at = datetime.now()
        return self.update(wf)

    def delete_by_workflow_id(self, workflow_id: str) -> bool:
        wf = self.find_by_workflow_id(workflow_id)
        if not wf:
            return False
        return self.delete(wf.id)
