# Assistant message repository - 助手对话消息仓储
from typing import List, Optional
from datetime import datetime
from logger import logger
from .base_repository import BaseRepository
from ..models.local_assistant_message import AssistantMessage
from ..local_connection import get_connection_manager


class AssistantMessageRepository(BaseRepository[AssistantMessage]):
    """助手对话消息仓储"""

    def __init__(self):
        super().__init__(AssistantMessage)

    def save(self, role: str, content: str, session_id: str = "default") -> int:
        """保存一条消息"""
        message = AssistantMessage(
            role=role,
            content=content,
            session_id=session_id,
            timestamp=datetime.now()
        )
        return self.create(message)

    def find_by_session(self, session_id: str = "default",
                        limit: int = 100) -> List[AssistantMessage]:
        """获取指定会话的消息历史"""
        with get_connection_manager().get_connection() as conn:
            cursor = conn.cursor()
            sql = f"""
                SELECT * FROM {self.table_name}
                WHERE session_id = {self.placeholder}
                ORDER BY timestamp ASC
                LIMIT {self.placeholder}
            """
            cursor.execute(sql, (session_id, limit))
            rows = cursor.fetchall()

            return [self.model_class.from_dict({k: row[k] for k in row.keys()})
                    for row in rows]

    def delete_message(self, message_id: int) -> bool:
        """删除指定消息 - 使用基类的 delete 方法"""
        return self.delete(message_id)

    def clear_session(self, session_id: str = "default") -> int:
        """清空指定会话的所有消息"""
        with get_connection_manager().get_connection() as conn:
            cursor = conn.cursor()
            sql = f"DELETE FROM {self.table_name} WHERE session_id = {self.placeholder}"
            cursor.execute(sql, (session_id,))
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
                # SQLite raises "duplicate column name" if column already exists
                if "duplicate column" not in str(e).lower():
                    raise

    def count_messages(self, session_id: str = "default") -> int:
        """Count non-summary messages in session."""
        with get_connection_manager().get_connection() as conn:
            cursor = conn.cursor()
            sql = (f"SELECT COUNT(*) as cnt FROM {self.table_name} "
                   f"WHERE session_id = {self.placeholder} AND role != 'summary'")
            cursor.execute(sql, (session_id,))
            row = cursor.fetchone()
            return row["cnt"] if row else 0

    def find_old_messages(self, session_id: str = "default",
                          limit: int = 30) -> List[AssistantMessage]:
        """Get oldest non-summary messages for compression."""
        with get_connection_manager().get_connection() as conn:
            cursor = conn.cursor()
            sql = (f"SELECT * FROM {self.table_name} "
                   f"WHERE session_id = {self.placeholder} AND role != 'summary' "
                   f"ORDER BY timestamp ASC LIMIT {self.placeholder}")
            cursor.execute(sql, (session_id, limit))
            rows = cursor.fetchall()
            return [self.model_class.from_dict({k: row[k] for k in row.keys()})
                    for row in rows]

    def delete_messages_by_ids(self, ids: List[int]) -> int:
        """Delete messages by ID list. Returns count deleted."""
        if not ids:
            return 0
        placeholders = ", ".join([self.placeholder] * len(ids))
        with get_connection_manager().get_connection() as conn:
            cursor = conn.cursor()
            sql = f"DELETE FROM {self.table_name} WHERE id IN ({placeholders})"
            cursor.execute(sql, ids)
            conn.commit()
            return cursor.rowcount

    def find_latest_summary(self, session_id: str = "default") -> Optional[AssistantMessage]:
        """Get most recent summary message, if any."""
        with get_connection_manager().get_connection() as conn:
            cursor = conn.cursor()
            sql = (f"SELECT * FROM {self.table_name} "
                   f"WHERE session_id = {self.placeholder} AND role = 'summary' "
                   f"ORDER BY timestamp DESC LIMIT 1")
            cursor.execute(sql, (session_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return self.model_class.from_dict({k: row[k] for k in row.keys()})

    def delete_pair(self, msg_id: int) -> int:
        """Delete a message and its paired counterpart.

        - If msg_id is a user message: delete it + the next assistant message.
        - If msg_id is an assistant message: delete it + the preceding user message.
        Returns count of messages deleted.
        """
        target = self.find_by_id(msg_id)
        if not target:
            return 0

        ids_to_delete = [msg_id]

        with get_connection_manager().get_connection() as conn:
            cursor = conn.cursor()
            if target.role == "user":
                # Find next assistant message after this user message
                # (use id, which is strictly monotonic — timestamp may collide in tight loops)
                sql = (f"SELECT id FROM {self.table_name} "
                       f"WHERE session_id = {self.placeholder} AND role = 'assistant' "
                       f"AND id > {self.placeholder} "
                       f"ORDER BY id ASC LIMIT 1")
                cursor.execute(sql, (target.session_id, msg_id))
                row = cursor.fetchone()
                if row:
                    ids_to_delete.append(row["id"])
            elif target.role == "assistant":
                # Find preceding user message
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
            cursor.execute(sql, ids_to_delete)
            conn.commit()
            return cursor.rowcount
