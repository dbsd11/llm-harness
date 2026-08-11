from typing import Optional, List
from .base_repository import BaseRepository
from ..models.workflow_task_template import WorkflowTaskTemplate


class WorkflowTaskTemplateRepository(BaseRepository[WorkflowTaskTemplate]):

    def __init__(self):
        super().__init__(WorkflowTaskTemplate)

    def find_by_template_id(self, template_id: str) -> Optional[WorkflowTaskTemplate]:
        results = self.find_by_criteria({"template_id": template_id})
        return results[0] if results else None

    def find_by_workflow_id(self, workflow_id: str) -> List[WorkflowTaskTemplate]:
        return self.find_by_criteria({"workflow_id": workflow_id})

    def delete_by_workflow_id(self, workflow_id: str) -> int:
        templates = self.find_by_workflow_id(workflow_id)
        count = 0
        for t in templates:
            if self.delete(t.id):
                count += 1
        return count
