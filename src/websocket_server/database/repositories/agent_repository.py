# Agent repository
from typing import Optional, List
from .base_repository import BaseRepository
from ..models.agent import Agent


class AgentRepository(BaseRepository[Agent]):
    """Repository for Agent model operations"""

    def __init__(self):
        super().__init__(Agent)
        self._ensure_columns()

    def _ensure_columns(self):
        """Migration: add scenario_id column if missing."""
        from database.connection import get_connection_manager
        try:
            conn_mgr = get_connection_manager()
            with conn_mgr.get_connection() as conn:
                cursor = conn.cursor()
                try:
                    cursor.execute(f"PRAGMA table_info({self.table_name})")
                    existing = {row["name"] for row in cursor.fetchall()}
                except Exception:
                    return
                if "scenario_id" not in existing:
                    try:
                        cursor.execute(
                            f"ALTER TABLE {self.table_name} ADD COLUMN scenario_id TEXT"
                        )
                        conn.commit()
                    except Exception:
                        pass
        except Exception:
            pass

    def find_by_agent_id(self, agent_id: str) -> Optional[Agent]:
        """Find agent by agent_id (UUID)"""
        results = self.find_by_criteria({"agent_id": agent_id})
        return results[0] if results else None

    def find_by_type(self, agent_type: str) -> List[Agent]:
        """Find agents by type"""
        return self.find_by_criteria({"agent_type": agent_type})

    def find_by_status(self, status: str) -> List[Agent]:
        """Find agents by status"""
        return self.find_by_criteria({"status": status})

    def find_by_scenario_id(self, scenario_id: str) -> List[Agent]:
        """Find agents registered for a scenario"""
        if not scenario_id:
            return []
        return self.find_by_criteria({"scenario_id": scenario_id})

    def delete_by_scenario_id(self, scenario_id: str) -> int:
        """Delete all agents registered for a scenario. Returns count deleted."""
        if not scenario_id:
            return 0
        from database.connection import get_connection_manager
        try:
            conn_mgr = get_connection_manager()
            with conn_mgr.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    f"DELETE FROM {self.table_name} WHERE scenario_id = {self.placeholder}",
                    (scenario_id,)
                )
                conn.commit()
                return cursor.rowcount
        except Exception as e:
            from logger import logger
            logger.error(f"Failed to delete agents for scenario {scenario_id}: {e}")
            return 0

    def update_status(self, agent_id: str, status: str) -> bool:
        """Update agent status"""
        agent = self.find_by_agent_id(agent_id)
        if not agent:
            return False
        agent.status = status
        return self.update(agent)
