# Scenario repository
from typing import Optional, List
from datetime import datetime
from .base_repository import BaseRepository
from ..models.scenario import Scenario


class ScenarioRepository(BaseRepository[Scenario]):
    """Repository for Scenario model operations"""

    def __init__(self):
        super().__init__(Scenario)

    def find_by_scenario_id(self, scenario_id: str) -> Optional[Scenario]:
        """Find scenario by scenario_id (UUID)"""
        results = self.find_by_criteria({"scenario_id": scenario_id})
        return results[0] if results else None

    def find_by_state(self, state: str) -> List[Scenario]:
        """Find scenarios by state"""
        return self.find_by_criteria({"state": state})

    def find_by_type(self, scenario_type: str) -> List[Scenario]:
        """Find scenarios by type"""
        return self.find_by_criteria({"scenario_type": scenario_type})

    def update_scenario_state(self, scenario_id: str, new_state: str) -> bool:
        """Update scenario state"""
        scenario = self.find_by_scenario_id(scenario_id)
        if not scenario:
            return False
        scenario.state = new_state
        scenario.updated_at = datetime.now()
        return self.update(scenario)

    def mark_as_started(self, scenario_id: str) -> bool:
        """Mark scenario as started"""
        scenario = self.find_by_scenario_id(scenario_id)
        if not scenario:
            return False
        scenario.state = "running"
        scenario.started_at = datetime.now()
        scenario.updated_at = datetime.now()
        return self.update(scenario)

    def mark_as_completed(self, scenario_id: str) -> bool:
        """Mark scenario as completed"""
        scenario = self.find_by_scenario_id(scenario_id)
        if not scenario:
            return False
        scenario.state = "completed"
        scenario.completed_at = datetime.now()
        scenario.updated_at = datetime.now()
        return self.update(scenario)

    def mark_as_failed(self, scenario_id: str) -> bool:
        """Mark scenario as failed"""
        scenario = self.find_by_scenario_id(scenario_id)
        if not scenario:
            return False
        scenario.state = "failed"
        scenario.completed_at = datetime.now()
        scenario.updated_at = datetime.now()
        return self.update(scenario)
