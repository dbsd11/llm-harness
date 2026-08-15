# Task repository
from typing import Optional, List
from datetime import datetime
from logger import logger
from .base_repository import BaseRepository
from ..models.task import Task


class TaskRepository(BaseRepository[Task]):
    """Repository for Task model operations"""

    def __init__(self):
        super().__init__(Task)
        self._ensure_columns()

    def _ensure_columns(self):
        """
        Add columns that may be missing from older DBs (migration).

        ponytail: ALTER TABLE ADD COLUMN with error suppression.
        No-op if columns already exist or table doesn't exist yet.
        """
        from database.connection import get_connection_manager
        cols_to_add = {
            "topic_id": "TEXT",
            "idempotency_key": "TEXT",
            "depends_on": "TEXT",
            "agent_name": "TEXT",
            "agent_role": "TEXT",
            "execution_duration": "REAL",
            "review_feedback": "TEXT",  # human supplementary feedback on rejection (manual_acceptance)
        }
        try:
            conn_mgr = get_connection_manager()
            with conn_mgr.get_connection() as conn:
                cursor = conn.cursor()
                # Get existing columns (fails silently if table doesn't exist yet)
                try:
                    cursor.execute(f"PRAGMA table_info({self.table_name})")
                    existing = {row["name"] for row in cursor.fetchall()}
                except Exception:
                    return  # Table doesn't exist yet, migration will run after creation
                for col, col_type in cols_to_add.items():
                    if col not in existing:
                        try:
                            cursor.execute(
                                f"ALTER TABLE {self.table_name} ADD COLUMN {col} {col_type}"
                            )
                            conn.commit()
                        except Exception:
                            pass  # Column may already exist in race conditions
        except Exception:
            pass  # DB not initialized yet

    def find_by_task_id(self, task_id: str) -> Optional[Task]:
        """Find task by task_id (UUID)"""
        results = self.find_by_criteria({"task_id": task_id})
        return results[0] if results else None

    def find_by_idempotency_key(self, key: str) -> Optional[Task]:
        """Find task by idempotency_key (幂等控制 lookup)"""
        if not key:
            return None
        results = self.find_by_criteria({"idempotency_key": key})
        return results[0] if results else None

    def find_by_topic_id(self, topic_id: str) -> List[Task]:
        """Find all subtasks belonging to a topic (子主题 lookup)"""
        if not topic_id:
            return []
        return self.find_by_criteria({"topic_id": topic_id})

    def find_by_state(self, state: str, tenant_id: str = None) -> List[Task]:
        """Find tasks by state (with optional tenant isolation)"""
        criteria = {"state": state}
        if tenant_id:
            return self._tenant_query(criteria, tenant_id)
        return self.find_by_criteria(criteria)

    def find_by_scenario_id(self, scenario_id: str, tenant_id: str = None) -> List[Task]:
        """Find tasks by scenario_id (with optional tenant isolation)"""
        criteria = {"scenario_id": scenario_id}
        if tenant_id:
            return self._tenant_query(criteria, tenant_id)
        return self.find_by_criteria(criteria)

    def find_by_parent_task_id(self, parent_task_id: str) -> List[Task]:
        """Find all subtasks of a parent task"""
        return self.find_by_criteria({"parent_task_id": parent_task_id})

    def update_task_state(self, task_id: str, new_state: str) -> bool:
        """Update task state"""
        task = self.find_by_task_id(task_id)
        if not task:
            return False
        task.state = new_state
        task.updated_at = datetime.now()
        return self.update(task)

    def increment_retry_count(self, task_id: str) -> bool:
        """Increment task retry count"""
        task = self.find_by_task_id(task_id)
        if not task:
            return False
        task.retry_count = (task.retry_count or 0) + 1
        task.updated_at = datetime.now()
        return self.update(task)

    def mark_as_started(self, task_id: str) -> bool:
        """Mark task as started"""
        task = self.find_by_task_id(task_id)
        if not task:
            return False
        task.state = "running"
        task.started_at = datetime.now()
        task.updated_at = datetime.now()
        return self.update(task)

    def mark_as_completed(self, task_id: str, result: str = None, agent_name: str = None,
                          agent_role: str = None, execution_duration: float = None) -> bool:
        """Mark task as completed with agent information

        使用事务确保原子性，防止竞态条件导致状态不一致。
        SQLite 使用 BEGIN IMMEDIATE 获取写锁，MySQL 使用 SELECT FOR UPDATE。
        """
        import os
        from database.connection import get_connection_manager

        conn_mgr = get_connection_manager()
        db_engine = os.getenv('DB_ENGINE', 'sqlite')

        with conn_mgr.get_connection() as conn:
            try:
                cursor = conn.cursor()

                # 根据数据库类型选择锁策略
                if db_engine == 'mysql':
                    # MySQL: 使用 SELECT FOR UPDATE
                    cursor.execute(
                        f"SELECT state FROM {self.table_name} WHERE task_id = %s FOR UPDATE",
                        (task_id,)
                    )
                else:
                    # SQLite: 使用 BEGIN IMMEDIATE 获取写锁
                    cursor.execute("BEGIN IMMEDIATE")
                    cursor.execute(
                        f"SELECT state FROM {self.table_name} WHERE task_id = ?",
                        (task_id,)
                    )

                row = cursor.fetchone()
                if not row:
                    if db_engine != 'mysql':
                        conn.commit()
                    return False

                current_state = row['state'] if isinstance(row, dict) else row[0]

                # 防止终态被覆盖（修复竞态条件）
                if current_state in ['success', 'failed', 'timeout', 'cancelled']:
                    logger.warning(
                        f"Task {task_id} already in terminal state: {current_state}, "
                        f"skipping mark_as_completed"
                    )
                    if db_engine != 'mysql':
                        conn.commit()
                    return False

                # 更新任务状态
                now = datetime.now()
                if db_engine == 'mysql':
                    cursor.execute(f"""
                        UPDATE {self.table_name}
                        SET state = %s, result = %s, error = NULL,
                            completed_at = %s, updated_at = %s,
                            agent_name = COALESCE(%s, agent_name),
                            agent_role = COALESCE(%s, agent_role),
                            execution_duration = COALESCE(%s, execution_duration)
                        WHERE task_id = %s
                    """, (
                        "success", result, now, now,
                        agent_name, agent_role, execution_duration,
                        task_id
                    ))
                else:
                    cursor.execute(f"""
                        UPDATE {self.table_name}
                        SET state = ?, result = ?, error = NULL,
                            completed_at = ?, updated_at = ?,
                            agent_name = COALESCE(?, agent_name),
                            agent_role = COALESCE(?, agent_role),
                            execution_duration = COALESCE(?, execution_duration)
                        WHERE task_id = ?
                    """, (
                        "success", result, now, now,
                        agent_name, agent_role, execution_duration,
                        task_id
                    ))

                conn.commit()
                return True

            except Exception as e:
                logger.error(f"mark_as_completed failed for {task_id}: {e}")
                conn.rollback()
                return False

    def mark_as_failed(self, task_id: str, error: str,
                       result: str = None, agent_name: str = None,
                       agent_role: str = None,
                       execution_duration: float = None) -> bool:
        """Mark task as failed, preserving the agent's output in `result`.

        使用事务确保原子性，防止竞态条件导致状态不一致。
        SQLite 使用 BEGIN IMMEDIATE 获取写锁，MySQL 使用 SELECT FOR UPDATE。
        """
        import os
        from database.connection import get_connection_manager

        conn_mgr = get_connection_manager()
        db_engine = os.getenv('DB_ENGINE', 'sqlite')

        with conn_mgr.get_connection() as conn:
            try:
                cursor = conn.cursor()

                # 根据数据库类型选择锁策略
                if db_engine == 'mysql':
                    # MySQL: 使用 SELECT FOR UPDATE
                    cursor.execute(
                        f"SELECT state FROM {self.table_name} WHERE task_id = %s FOR UPDATE",
                        (task_id,)
                    )
                else:
                    # SQLite: 使用 BEGIN IMMEDIATE 获取写锁
                    cursor.execute("BEGIN IMMEDIATE")
                    cursor.execute(
                        f"SELECT state FROM {self.table_name} WHERE task_id = ?",
                        (task_id,)
                    )

                row = cursor.fetchone()
                if not row:
                    if db_engine != 'mysql':
                        conn.commit()
                    return False

                current_state = row['state'] if isinstance(row, dict) else row[0]

                # 防止终态被覆盖（修复竞态条件）
                if current_state in ['success', 'failed', 'timeout', 'cancelled']:
                    logger.warning(
                        f"Task {task_id} already in terminal state: {current_state}, "
                        f"skipping mark_as_failed"
                    )
                    if db_engine != 'mysql':
                        conn.commit()
                    return False

                # 更新任务状态
                now = datetime.now()
                if db_engine == 'mysql':
                    cursor.execute(f"""
                        UPDATE {self.table_name}
                        SET state = %s, error = %s, result = %s,
                            agent_name = %s, agent_role = %s,
                            execution_duration = %s,
                            completed_at = %s, updated_at = %s
                        WHERE task_id = %s
                    """, ("failed", error, result,
                          agent_name, agent_role, execution_duration,
                          now, now, task_id))
                else:
                    cursor.execute(f"""
                        UPDATE {self.table_name}
                        SET state = ?, error = ?, result = ?,
                            agent_name = ?, agent_role = ?,
                            execution_duration = ?,
                            completed_at = ?, updated_at = ?
                        WHERE task_id = ?
                    """, ("failed", error, result,
                          agent_name, agent_role, execution_duration,
                          now, now, task_id))

                conn.commit()
                return True

            except Exception as e:
                logger.error(f"mark_as_failed failed for {task_id}: {e}")
                conn.rollback()
                return False

    def mark_as_pending_review(self, task_id: str, result: str = None,
                               agent_name: str = None, agent_role: str = None,
                               execution_duration: float = None) -> bool:
        """Route a finished task to human acceptance (manual_acceptance scenario).

        Preserves the agent's outcome in `result` for the reviewer. The task is
        execution-complete but NOT lifecycle-terminal - it can still move to
        success/failed via mark_as_reviewed. completed_at is left unset.
        Idempotent: a task already pending_review is a no-op success.
        """
        task = self.find_by_task_id(task_id)
        if not task:
            return False
        if task.state == "pending_review":
            return True
        task.state = "pending_review"
        task.result = result
        if agent_name is not None:
            task.agent_name = agent_name
        if agent_role is not None:
            task.agent_role = agent_role
        if execution_duration is not None:
            task.execution_duration = execution_duration
        task.updated_at = datetime.now()
        return self.update(task)

    def mark_as_reviewed(self, task_id: str, passed: bool,
                         feedback: str = None) -> bool:
        """Apply a human acceptance decision to a PENDING_REVIEW task.

        passed=True  -> state `success` (completed_at set); feedback is an optional
                        reviewer comment stored in review_feedback.
        passed=False -> state `failed` (error="Manual rejection", completed_at set);
                        feedback is REQUIRED and stored in review_feedback.

        Returns False if the task is not pending_review (only reviewed tasks can be
        accepted/rejected) or if a rejection lacks feedback.
        """
        task = self.find_by_task_id(task_id)
        if not task:
            return False
        if task.state != "pending_review":
            logger.warning(f"mark_as_reviewed: task {task_id} not pending_review "
                           f"(state={task.state}); rejecting transition")
            return False
        if not passed and not (feedback and feedback.strip()):
            logger.warning(f"mark_as_reviewed: rejection of {task_id} requires feedback")
            return False
        if passed:
            task.state = "success"
        else:
            task.state = "failed"
            task.error = "Manual rejection"
        task.review_feedback = (feedback.strip() if feedback else None) or None
        task.completed_at = datetime.now()
        task.updated_at = datetime.now()
        return self.update(task)

    def mark_as_cancelled(self, task_id: str, reason: str = "") -> bool:
        """Mark a task cancelled (terminal). Used for superseded dependents during
        sub-tree re-derivation and for scenario-stop cleanup."""
        task = self.find_by_task_id(task_id)
        if not task:
            return False
        task.state = "cancelled"
        task.error = reason or None
        task.completed_at = datetime.now()
        task.updated_at = datetime.now()
        return self.update(task)

    def find_pending_review_by_scenario(self, scenario_id: str) -> List[Task]:
        """All tasks awaiting human acceptance in a scenario."""
        if not scenario_id:
            return []
        return self.find_by_criteria(
            {"scenario_id": scenario_id, "state": "pending_review"})

    def count_pending_review_by_scenario(self, scenario_id: str) -> int:
        """Number of tasks still awaiting acceptance in a scenario."""
        if not scenario_id:
            return 0
        return self.count(
            {"scenario_id": scenario_id, "state": "pending_review"})

    def find_dependents_by_task_id(self, task_id: str,
                                   tenant_id: str = None) -> List[Task]:
        """Reverse-dependency lookup: tasks whose depends_on JSON array contains
        task_id. Used for sub-tree re-derivation (manual_acceptance)."""
        if not task_id:
            return []
        from database.connection import get_connection_manager
        pattern = f'%"{task_id}"%'
        ph = self.placeholder
        sql = (f"SELECT * FROM {self.table_name} "
               f"WHERE depends_on LIKE {ph}")
        params = [pattern]

        if tenant_id:
            sql += f" AND (tenant_id = {ph} OR tenant_id IS NULL)"
            params.append(tenant_id)

        try:
            conn_mgr = get_connection_manager()
            with conn_mgr.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(sql, params)
                rows = cursor.fetchall()
                return [self.model_class.from_dict({k: row[k] for k in row.keys()})
                        for row in rows]
        except Exception as e:
            logger.error(f"find_dependents_by_task_id failed: {e}")
            return []
