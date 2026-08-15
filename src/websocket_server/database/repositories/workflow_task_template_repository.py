from typing import Optional, List, Dict
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

    def count_by_workflow_ids(self, workflow_ids: List[str],
                              tenant_id: str = None) -> Dict[str, int]:
        """批量查询多个 workflow 的步骤数，返回 {workflow_id: count}。"""
        if not workflow_ids:
            return {}
        ph = self.placeholder
        placeholders = ", ".join([ph] * len(workflow_ids))
        sql = (
            f"SELECT workflow_id, COUNT(*) as cnt FROM {self.table_name} "
            f"WHERE workflow_id IN ({placeholders})"
        )
        params = list(workflow_ids)

        if tenant_id:
            sql += f" AND (tenant_id = {ph} OR tenant_id IS NULL)"
            params.append(tenant_id)

        sql += " GROUP BY workflow_id"

        from ..connection import get_connection_manager
        cm = get_connection_manager()
        with cm.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            return {row["workflow_id"]: row["cnt"] for row in cursor.fetchall()}

    def delete_by_workflow_id(self, workflow_id: str) -> int:
        templates = self.find_by_workflow_id(workflow_id)
        count = 0
        for t in templates:
            if self.delete(t.id):
                count += 1
        return count
