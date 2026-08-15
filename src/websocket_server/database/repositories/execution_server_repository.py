# ExecutionServer repository
import os
import json
from datetime import datetime
from typing import Optional, List
from .base_repository import BaseRepository
from ..models.execution_server import ExecutionServer


class ExecutionServerRepository(BaseRepository[ExecutionServer]):
    """Repository for the execution_servers registry table."""

    def __init__(self):
        super().__init__(ExecutionServer)
        self._ensure_columns()

    def upsert(self, server_id: str, name: str = None, status: str = "offline",
               total_quota: int = 0, running_count: int = 0,
               env_info: dict = None, connected: bool = False,
               last_heartbeat: datetime = None,
               source: str = None,
               tenant_id: str = None) -> None:
        """Insert or replace a server row (keyed on server_id)."""
        env_json = json.dumps(env_info, ensure_ascii=False) if env_info else "{}"
        hb = last_heartbeat or datetime.now()
        now = datetime.now()
        source = source or "execution_server"

        if self.db_engine == "mysql":
            sql = (
                f"INSERT INTO {self.table_name} "
                "(server_id, name, status, source, total_quota, running_count, "
                "env_info, last_heartbeat, connected, updated_at, tenant_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE name=VALUES(name), status=VALUES(status), "
                "source=VALUES(source), total_quota=VALUES(total_quota), "
                "running_count=VALUES(running_count), env_info=VALUES(env_info), "
                "last_heartbeat=VALUES(last_heartbeat), connected=VALUES(connected), "
                "updated_at=VALUES(updated_at), tenant_id=VALUES(tenant_id)"
            )
        else:  # sqlite
            sql = (
                f"INSERT OR REPLACE INTO {self.table_name} "
                "(server_id, name, status, source, total_quota, running_count, "
                "env_info, last_heartbeat, connected, updated_at, tenant_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )

        values = (server_id, name or server_id, status, source, int(total_quota),
                  int(running_count), env_json, hb, bool(connected), now, tenant_id)

        from ..connection import get_connection_manager
        cm = get_connection_manager()
        with cm.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, values)
            conn.commit()

    def update_status(self, server_id: str, status: str, running_count: int,
                      connected: bool, env_info: dict = None,
                      last_heartbeat: datetime = None) -> None:
        """Update the volatile fields written on each heartbeat."""
        hb = last_heartbeat or datetime.now()
        now = datetime.now()
        ph = self.placeholder

        if env_info is not None:
            env_json = json.dumps(env_info, ensure_ascii=False)
            sql = (
                f"UPDATE {self.table_name} SET status={ph}, running_count={ph}, "
                f"connected={ph}, env_info={ph}, last_heartbeat={ph}, updated_at={ph} "
                f"WHERE server_id={ph}"
            )
            values = (status, int(running_count), bool(connected), env_json, hb, now, server_id)
        else:
            sql = (
                f"UPDATE {self.table_name} SET status={ph}, running_count={ph}, "
                f"connected={ph}, last_heartbeat={ph}, updated_at={ph} WHERE server_id={ph}"
            )
            values = (status, int(running_count), bool(connected), hb, now, server_id)

        from ..connection import get_connection_manager
        cm = get_connection_manager()
        with cm.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, values)
            conn.commit()

    def mark_offline(self, server_id: str) -> None:
        """Mark a single server offline (called on WS disconnect)."""
        ph = self.placeholder
        sql = (
            f"UPDATE {self.table_name} SET status={ph}, connected={ph}, "
            f"running_count={ph}, updated_at={ph} WHERE server_id={ph}"
        )
        from ..connection import get_connection_manager
        cm = get_connection_manager()
        with cm.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, ("offline", False, 0, datetime.now(), server_id))
            conn.commit()

    def mark_all_offline(self) -> None:
        """Clear stale connected=True rows at WS-server boot."""
        ph = self.placeholder
        sql = (
            f"UPDATE {self.table_name} SET status={ph}, connected={ph}, "
            f"running_count={ph}, updated_at={ph}"
        )
        from ..connection import get_connection_manager
        cm = get_connection_manager()
        with cm.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, ("offline", False, 0, datetime.now()))
            conn.commit()

    def mark_stale_offline(self, stale_before: datetime) -> int:
        """Mark connected servers whose heartbeat is older than stale_offline."""
        ph = self.placeholder
        sql = (
            f"UPDATE {self.table_name} SET status={ph}, connected={ph}, "
            f"running_count={ph}, updated_at={ph} "
            f"WHERE connected={ph} AND last_heartbeat < {ph}"
        )
        from ..connection import get_connection_manager
        cm = get_connection_manager()
        with cm.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, ("offline", False, 0, datetime.now(),
                                 True, stale_before))
            conn.commit()
            return cursor.rowcount

    def find_by_server_id(self, server_id: str) -> Optional[ExecutionServer]:
        return self.find_by_id(server_id)

    def list_all(self) -> List[ExecutionServer]:
        return self.find_all(order_by="server_id ASC")

    def delete(self, server_id: str, tenant_id: str = None) -> bool:
        """Delete a server row. Only call for offline servers — a connected
        server would be re-upserted on its next heartbeat.

        Returns True if a row was deleted.
        """
        ph = self.placeholder
        sql = f"DELETE FROM {self.table_name} WHERE server_id={ph}"
        params = [server_id]
        if tenant_id:
            sql += f" AND (tenant_id={ph} OR tenant_id IS NULL)"
            params.append(tenant_id)
        from ..connection import get_connection_manager
        cm = get_connection_manager()
        with cm.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            conn.commit()
            return cursor.rowcount > 0

    def delete_offline(self) -> int:
        """Delete all disconnected servers (historical / useless rows).

        Connected servers are never deleted here — they'd be re-created by the
        next heartbeat. Returns the count deleted.
        """
        ph = self.placeholder
        sql = f"DELETE FROM {self.table_name} WHERE connected={ph}"
        from ..connection import get_connection_manager
        cm = get_connection_manager()
        with cm.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, (False,))
            conn.commit()
            return cursor.rowcount

    def delete_offline_by_tenant(self, tenant_id: str) -> int:
        """Delete disconnected servers for a specific tenant.

        Returns the count deleted.
        """
        ph = self.placeholder
        sql = f"DELETE FROM {self.table_name} WHERE connected={ph} AND (tenant_id={ph} OR tenant_id IS NULL)"
        from ..connection import get_connection_manager
        cm = get_connection_manager()
        with cm.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, (False, tenant_id))
            conn.commit()
            return cursor.rowcount

    def _ensure_columns(self):
        """Add columns that may be missing from older DBs (migration).

        ponytail: ALTER TABLE ADD COLUMN with error suppression.
        No-op if columns already exist or table doesn't exist yet.
        """
        from database.connection import get_connection_manager
        cols_to_add = {
            "source": "TEXT",
        }
        try:
            conn_mgr = get_connection_manager()
            with conn_mgr.get_connection() as conn:
                cursor = conn.cursor()
                try:
                    cursor.execute(f"PRAGMA table_info({self.table_name})")
                    existing = {row["name"] for row in cursor.fetchall()}
                except Exception:
                    return  # Table doesn't exist yet; created fresh on init.
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
            pass  # DB not initialized yet
