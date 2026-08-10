# Message repository
from typing import List
from .base_repository import BaseRepository
from ..connection import get_connection_manager
from ..models.message import Message


class MessageRepository(BaseRepository[Message]):
    """Repository for Message model operations"""

    def __init__(self):
        super().__init__(Message)
        self._ensure_columns()

    def _ensure_columns(self):
        """Add the `acked` column to older DBs (migration), and backfill
        historical dispatch rows as acked=1 so they are not replayed by the
        ack-after-execute consumer. Idempotent.

        ponytail: ALTER TABLE ADD COLUMN + backfill, both no-ops once applied.
        """
        try:
            cm = get_connection_manager()
            with cm.get_connection() as conn:
                cursor = conn.cursor()
                try:
                    cursor.execute(f"PRAGMA table_info({self.table_name})")
                    existing = {row["name"] for row in cursor.fetchall()}
                except Exception:
                    return  # table doesn't exist yet; created later by init_database
                if "acked" not in existing:
                    try:
                        cursor.execute(
                            f"ALTER TABLE {self.table_name} ADD COLUMN acked INTEGER"
                        )
                        conn.commit()
                    except Exception:
                        pass  # race / already exists
                # Backfill: treat all pre-existing dispatches as already acked
                # (historical; orphaned-in-flight ones are reclaimed by the
                # startup orphan sweep, not by redelivery).
                try:
                    cursor.execute(
                        f"UPDATE {self.table_name} SET acked=1 "
                        f"WHERE message_type='dispatch' AND acked IS NULL"
                    )
                    conn.commit()
                except Exception:
                    pass
        except Exception:
            pass

    def find_by_scenario_id(self, scenario_id: str, limit: int = 200) -> List[Message]:
        """Find all messages for a scenario, ordered by timestamp desc"""
        if not scenario_id:
            return []
        return self.find_by_criteria({"scenario_id": scenario_id}, limit=limit)

    def find_pending_messages(self, consumer_id: str, scenario_id: str,
                              message_type: str, limit: int = 100) -> List[Message]:
        """Find unconsumed messages for a BOT (id > consumer's offset), ordered by id ASC."""
        from .consumer_offset_repository import ConsumerOffsetRepository
        offset_repo = ConsumerOffsetRepository()
        offset = offset_repo.get_offset(consumer_id)

        criteria = {
            "scenario_id": scenario_id,
            "message_type": message_type,
            "id": {"operator": ">", "condition": offset},
        }
        return self.find_by_criteria(criteria, order_by="id ASC", limit=limit)

    def find_pending_dispatch_global(self, consumer_id: str, limit: int = 100) -> List[Message]:
        """Find unconsumed dispatch messages across ALL scenarios (global consumer).

        Used by the CentralDispatcher: queries dispatch rows with id > the
        global consumer offset, ordered id ASC, regardless of scenario_id.
        """
        from .consumer_offset_repository import ConsumerOffsetRepository
        offset_repo = ConsumerOffsetRepository()
        offset = offset_repo.get_offset(consumer_id)

        criteria = {
            "message_type": "dispatch",
            "id": {"operator": ">", "condition": offset},
        }
        return self.find_by_criteria(criteria, order_by="id ASC", limit=limit)

    def find_unacked_dispatch(self, limit: int = 100) -> List[Message]:
        """Find dispatch messages not yet acked (ack-after-execute consumer).

        Ordered by id ASC. NULL acked is treated as unacked (covers rows
        written before the column existed and not caught by the backfill).
        """
        cm = get_connection_manager()
        with cm.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT * FROM {self.table_name} "
                f"WHERE message_type='dispatch' AND (acked IS NULL OR acked=0) "
                f"ORDER BY id ASC LIMIT ?",
                (limit,),
            )
            rows = cursor.fetchall()
            return [Message.from_dict({k: r[k] for k in r.keys()}) for r in rows]

    def ack_message(self, message_id: int) -> None:
        """Mark a dispatch message as acked (execution completed)."""
        cm = get_connection_manager()
        with cm.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE {self.table_name} SET acked=1 WHERE id=?",
                (message_id,),
            )
            conn.commit()

    def max_message_id(self) -> int:
        """Return MAX(id) over the messages table, or 0 if empty."""
        cm = get_connection_manager()
        with cm.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT COALESCE(MAX(id), 0) AS m FROM {self.table_name}")
            row = cursor.fetchone()
            return int(row["m"]) if row else 0
