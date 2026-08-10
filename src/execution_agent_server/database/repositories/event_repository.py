# Event repository
from typing import List, Optional
from datetime import datetime
from .base_repository import BaseRepository
from ..models.event import Event


class EventRepository(BaseRepository[Event]):
    """Repository for Event model operations"""

    def __init__(self):
        super().__init__(Event)
        self._ensure_columns()

    def _ensure_columns(self):
        """Migration: add trace_id and metadata columns if missing."""
        from database.connection import get_connection_manager
        cols_to_add = {"trace_id": "TEXT", "metadata": "TEXT"}
        try:
            conn_mgr = get_connection_manager()
            with conn_mgr.get_connection() as conn:
                cursor = conn.cursor()
                try:
                    cursor.execute(f"PRAGMA table_info({self.table_name})")
                    existing = {row["name"] for row in cursor.fetchall()}
                except Exception:
                    return
                for col, col_type in cols_to_add.items():
                    if col not in existing:
                        try:
                            cursor.execute(
                                f"ALTER TABLE {self.table_name} ADD COLUMN {col} {col_type}"
                            )
                            conn.commit()
                        except Exception:
                            pass
        except Exception:
            pass

    def create_event(self, event_type: str, data: str,
                     trace_id: str = None, metadata: str = None) -> int:
        """Create a new event with optional trace context"""
        event = Event(
            event_type=event_type,
            data=data,
            trace_id=trace_id,
            metadata=metadata,
            timestamp=datetime.now()
        )
        return self.create(event)

    def find_by_trace_id(self, trace_id: str, limit: int = 100) -> List[Event]:
        """Find all events in a trace (跨事件追踪)"""
        if not trace_id:
            return []
        return self.find_by_criteria({"trace_id": trace_id}, limit=limit)

    def find_by_event_type(self, event_type: str, limit: int = 100) -> List[Event]:
        """Find events by event type"""
        return self.find_by_criteria({"event_type": event_type}, limit=limit)

    def find_by_event_type_prefix(self, prefix: str, limit: int = 100) -> List[Event]:
        """Find events by event type prefix (e.g., 'task.*')"""
        placeholder = '%s' if self.db_engine == 'mysql' else '?'
        sql = f"SELECT * FROM {self.table_name} WHERE event_type LIKE {placeholder} ORDER BY timestamp DESC LIMIT {placeholder}"

        from ..connection import get_connection_manager
        connection_manager = get_connection_manager()
        with connection_manager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, (f"{prefix}%", limit))
            rows = cursor.fetchall()

            result = []
            for row in rows:
                data = {key: row[key] for key in row.keys()}
                result.append(self.model_class.from_dict(data))

            return result

    def find_recent(self, limit: int = 100) -> List[Event]:
        """Find recent events"""
        return self.find_all(limit=limit)
