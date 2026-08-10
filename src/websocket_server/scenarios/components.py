# Scenario components - reusable building blocks (组件定义)
from typing import Dict, Any, Optional
from abc import ABC, abstractmethod
from datetime import datetime


class ScenarioComponent(ABC):
    """
    Reusable component in a scenario (组件定义).

    Components encapsulate a piece of scenario logic that can be
    composed into flows. Each component has input/output contracts.
    """

    def __init__(self, component_id: str, name: str, config: Dict[str, Any] = None):
        self.component_id = component_id
        self.name = name
        self.config = config or {}

    @abstractmethod
    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute component logic.

        Args:
            context: Input context from previous steps

        Returns:
            Output dict for downstream steps
        """
        pass

    def get_component_type(self) -> str:
        """Return component type identifier"""
        return self.__class__.__name__

    def to_dict(self) -> Dict[str, Any]:
        return {
            "component_id": self.component_id,
            "name": self.name,
            "type": self.get_component_type(),
            "config": self.config,
        }


class ComponentResult:
    """Result of a component execution (组件化输出)"""

    def __init__(self, component_id: str, status: str = "success",
                 data: Dict[str, Any] = None, error: str = None):
        self.component_id = component_id
        self.status = status  # "success", "failed", "skipped"
        self.data = data or {}
        self.error = error
        self.executed_at = datetime.now()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "component_id": self.component_id,
            "status": self.status,
            "data": self.data,
            "error": self.error,
            "executed_at": self.executed_at.isoformat(),
        }


class ScenarioOutput:
    """
    Structured, componentized scenario output (场景模式输出: 组件化).

    Replaces flat dict results with named component results.
    """

    def __init__(self, scenario_id: str):
        self.scenario_id = scenario_id
        self.component_results: Dict[str, ComponentResult] = {}

    def add_result(self, result: ComponentResult):
        self.component_results[result.component_id] = result

    @property
    def success(self) -> bool:
        return all(r.status == "success" for r in self.component_results.values())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "success": self.success,
            "components": {
                cid: r.to_dict()
                for cid, r in self.component_results.items()
            },
        }
