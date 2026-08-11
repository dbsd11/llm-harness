from typing import Optional, List
from datetime import datetime
from logger import logger
from .base_repository import BaseRepository
from ..models.workflow_execution import WorkflowExecution


class WorkflowExecutionRepository(BaseRepository[WorkflowExecution]):

    def __init__(self):
        super().__init__(WorkflowExecution)
        self._ensure_columns()

    def _ensure_columns(self):
        from database.connection import get_connection_manager
        cols_to_add = {
            "scenario_id": "TEXT",
            "parent_task_id": "TEXT",
        }
        try:
            conn_mgr = get_connection_manager()
            with conn_mgr.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(f"PRAGMA table_info({self.table_name})")
                existing = {row[1] for row in cursor.fetchall()}
                for col, col_type in cols_to_add.items():
                    if col not in existing:
                        try:
                            cursor.execute(
                                f"ALTER TABLE {self.table_name} ADD COLUMN {col} {col_type}")
                            logger.info(f"Added column {col} to {self.table_name}")
                        except Exception as e:
                            logger.warning(f"Failed to add column {col}: {e}")
                conn.commit()
        except Exception as e:
            logger.warning(f"_ensure_columns for {self.table_name}: {e}")

    def find_by_execution_id(self, execution_id: str) -> Optional[WorkflowExecution]:
        results = self.find_by_criteria({"execution_id": execution_id})
        return results[0] if results else None

    def find_by_workflow_id(self, workflow_id: str, limit: int = 50) -> List[WorkflowExecution]:
        return self.find_by_criteria({"workflow_id": workflow_id}, limit=limit)

    def find_by_state(self, state: str, limit: int = 100) -> List[WorkflowExecution]:
        return self.find_by_criteria({"state": state}, limit=limit)

    def find_recent(self, limit: int = 50) -> List[WorkflowExecution]:
        return self.find_all(order_by="created_at DESC", limit=limit)

    def update_scenario_link(self, execution_id: str, scenario_id: str,
                             parent_task_id: str) -> bool:
        exe = self.find_by_execution_id(execution_id)
        if not exe:
            return False
        exe.scenario_id = scenario_id
        exe.parent_task_id = parent_task_id
        exe.updated_at = datetime.now()
        return self.update(exe)

    def update_progress(self, execution_id: str, completed: int, failed: int,
                        task_results_json: str) -> bool:
        exe = self.find_by_execution_id(execution_id)
        if not exe:
            return False
        exe.completed_steps = completed
        exe.failed_steps = failed
        exe.task_results = task_results_json
        exe.updated_at = datetime.now()
        return self.update(exe)

    def mark_as_running(self, execution_id: str) -> bool:
        exe = self.find_by_execution_id(execution_id)
        if not exe:
            return False
        exe.state = "running"
        exe.started_at = datetime.now()
        exe.updated_at = datetime.now()
        return self.update(exe)

    def mark_as_completed(self, execution_id: str) -> bool:
        exe = self.find_by_execution_id(execution_id)
        if not exe:
            return False
        exe.state = "completed"
        exe.completed_at = datetime.now()
        exe.updated_at = datetime.now()
        return self.update(exe)

    def mark_as_failed(self, execution_id: str, error: str) -> bool:
        exe = self.find_by_execution_id(execution_id)
        if not exe:
            return False
        exe.state = "failed"
        exe.error = error
        exe.completed_at = datetime.now()
        exe.updated_at = datetime.now()
        return self.update(exe)

    def mark_as_cancelled(self, execution_id: str) -> bool:
        exe = self.find_by_execution_id(execution_id)
        if not exe:
            return False
        exe.state = "cancelled"
        exe.completed_at = datetime.now()
        exe.updated_at = datetime.now()
        return self.update(exe)
