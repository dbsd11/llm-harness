# Assistant message repository - 助手对话消息仓储
from typing import List, Optional
from datetime import datetime
from logger import logger
from .base_repository import BaseRepository
from ..models.assistant_message import AssistantMessage
from ..connection import get_connection_manager


class AssistantMessageRepository(BaseRepository[AssistantMessage]):
    """助手对话消息仓储"""

    def __init__(self):
        super().__init__(AssistantMessage)

    def save(self, role: str, content: str, session_id: str = "default",
             tenant_id: str = None) -> int:
        """保存一条消息"""
        message = AssistantMessage(
            role=role,
            content=content,
            session_id=session_id,
            timestamp=datetime.now(),
            tenant_id=tenant_id,
        )
        return self.create(message)

    def find_by_session(self, session_id: str = "default",
                        limit: int = 100,
                        tenant_id: str = None) -> List[AssistantMessage]:
        """获取指定会话的消息历史"""
        with get_connection_manager().get_connection() as conn:
            cursor = conn.cursor()
            sql = f"""
                SELECT * FROM {self.table_name}
                WHERE session_id = {self.placeholder}
            """
            params = [session_id]

            if tenant_id:
                sql += f" AND (tenant_id = {self.placeholder} OR tenant_id IS NULL)"
                params.append(tenant_id)

            sql += f" ORDER BY timestamp ASC LIMIT {self.placeholder}"
            params.append(limit)

            cursor.execute(sql, params)
            rows = cursor.fetchall()

            return [self.model_class.from_dict({k: row[k] for k in row.keys()})
                    for row in rows]

    def delete_message(self, message_id: int) -> bool:
        """删除指定消息 - 使用基类的 delete 方法"""
        return self.delete(message_id)

    def clear_session(self, session_id: str = "default",
                      tenant_id: str = None) -> int:
        """清空指定会话的所有消息"""
        with get_connection_manager().get_connection() as conn:
            cursor = conn.cursor()
            sql = f"DELETE FROM {self.table_name} WHERE session_id = {self.placeholder}"
            params = [session_id]

            if tenant_id:
                sql += f" AND (tenant_id = {self.placeholder} OR tenant_id IS NULL)"
                params.append(tenant_id)

            cursor.execute(sql, params)
            conn.commit()
            return cursor.rowcount

    def _ensure_columns(self):
        """Idempotent migration: add summary column if missing (SQLite)."""
        with get_connection_manager().get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    f"ALTER TABLE {self.table_name} ADD COLUMN summary TEXT"
                )
                conn.commit()
            except Exception as e:
                if "duplicate column" not in str(e).lower():
                    raise

    def count_messages(self, session_id: str = "default",
                       tenant_id: str = None) -> int:
        """Count non-summary messages in session."""
        with get_connection_manager().get_connection() as conn:
            cursor = conn.cursor()
            sql = (f"SELECT COUNT(*) as cnt FROM {self.table_name} "
                   f"WHERE session_id = {self.placeholder} AND role != 'summary'")
            params = [session_id]

            if tenant_id:
                sql += f" AND (tenant_id = {self.placeholder} OR tenant_id IS NULL)"
                params.append(tenant_id)

            cursor.execute(sql, params)
            row = cursor.fetchone()
            return row["cnt"] if row else 0

    def find_old_messages(self, session_id: str = "default",
                          limit: int = 30,
                          tenant_id: str = None) -> List[AssistantMessage]:
        """Get oldest non-summary messages for compression."""
        with get_connection_manager().get_connection() as conn:
            cursor = conn.cursor()
            sql = (f"SELECT * FROM {self.table_name} "
                   f"WHERE session_id = {self.placeholder} AND role != 'summary'")
            params = [session_id]

            if tenant_id:
                sql += f" AND (tenant_id = {self.placeholder} OR tenant_id IS NULL)"
                params.append(tenant_id)

            sql += f" ORDER BY timestamp ASC LIMIT {self.placeholder}"
            params.append(limit)

            cursor.execute(sql, params)
            rows = cursor.fetchall()
            return [self.model_class.from_dict({k: row[k] for k in row.keys()})
                    for row in rows]

    def delete_messages_by_ids(self, ids: List[int],
                               tenant_id: str = None) -> int:
        """Delete messages by ID list. Returns count deleted."""
        if not ids:
            return 0
        placeholders = ", ".join([self.placeholder] * len(ids))
        with get_connection_manager().get_connection() as conn:
            cursor = conn.cursor()
            sql = f"DELETE FROM {self.table_name} WHERE id IN ({placeholders})"
            params = list(ids)

            if tenant_id:
                sql += f" AND (tenant_id = {self.placeholder} OR tenant_id IS NULL)"
                params.append(tenant_id)

            cursor.execute(sql, params)
            conn.commit()
            return cursor.rowcount

    def find_latest_summary(self, session_id: str = "default",
                            tenant_id: str = None) -> Optional[AssistantMessage]:
        """Get most recent summary message, if any."""
        with get_connection_manager().get_connection() as conn:
            cursor = conn.cursor()
            sql = (f"SELECT * FROM {self.table_name} "
                   f"WHERE session_id = {self.placeholder} AND role = 'summary'")
            params = [session_id]

            if tenant_id:
                sql += f" AND (tenant_id = {self.placeholder} OR tenant_id IS NULL)"
                params.append(tenant_id)

            sql += f" ORDER BY timestamp DESC LIMIT 1"

            cursor.execute(sql, params)
            row = cursor.fetchone()
            if not row:
                return None
            return self.model_class.from_dict({k: row[k] for k in row.keys()})

    def delete_pair(self, msg_id: int, tenant_id: str = None) -> int:
        """Delete a message and its paired counterpart.

        - If msg_id is a user message: delete it + the next assistant message.
        - If msg_id is an assistant message: delete it + the preceding user message.
        Returns count of messages deleted.
        """
        target = self.find_by_id(msg_id)
        if not target:
            return 0

        if tenant_id and getattr(target, 'tenant_id', None) and target.tenant_id != tenant_id:
            logger.warning(f"delete_pair: tenant mismatch for message {msg_id}")
            return 0

        ids_to_delete = [msg_id]

        with get_connection_manager().get_connection() as conn:
            cursor = conn.cursor()
            if target.role == "user":
                sql = (f"SELECT id FROM {self.table_name} "
                       f"WHERE session_id = {self.placeholder} AND role = 'assistant' "
                       f"AND id > {self.placeholder} "
                       f"ORDER BY id ASC LIMIT 1")
                cursor.execute(sql, (target.session_id, msg_id))
                row = cursor.fetchone()
                if row:
                    ids_to_delete.append(row["id"])
            elif target.role == "assistant":
                sql = (f"SELECT id FROM {self.table_name} "
                       f"WHERE session_id = {self.placeholder} AND role = 'user' "
                       f"AND id < {self.placeholder} "
                       f"ORDER BY id DESC LIMIT 1")
                cursor.execute(sql, (target.session_id, msg_id))
                row = cursor.fetchone()
                if row:
                    ids_to_delete.append(row["id"])

            placeholders = ", ".join([self.placeholder] * len(ids_to_delete))
            sql = f"DELETE FROM {self.table_name} WHERE id IN ({placeholders})"
            params = list(ids_to_delete)

            if tenant_id:
                sql += f" AND (tenant_id = {self.placeholder} OR tenant_id IS NULL)"
                params.append(tenant_id)

            cursor.execute(sql, params)
            conn.commit()
            return cursor.rowcount
