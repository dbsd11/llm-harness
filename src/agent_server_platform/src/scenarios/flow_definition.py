# Flow definition - step-by-step scenario workflow (流程定义)
from typing import Dict, List, Optional, Any


class FlowStep:
    """A single step in a scenario flow"""

    def __init__(self, step_id: str, agent_type: str, goal: str,
                 depends_on: List[str] = None, config: Dict[str, Any] = None):
        self.step_id = step_id
        self.agent_type = agent_type  # "scheduling" or "execution"
        self.goal = goal
        self.depends_on = depends_on or []
        self.config = config or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "agent_type": self.agent_type,
            "goal": self.goal,
            "depends_on": self.depends_on,
            "config": self.config,
        }


class FlowDefinition:
    """
    Defines a step-by-step flow for a scenario (流程定义).

    ponytail: topological sort for dependency ordering.
    No DAG library — stdlib is enough for acyclic step graphs.
    """

    def __init__(self, flow_id: str, name: str, description: str = ""):
        self.flow_id = flow_id
        self.name = name
        self.description = description
        self.steps: Dict[str, FlowStep] = {}

    def add_step(self, step: FlowStep) -> 'FlowDefinition':
        """Add a step to the flow. Returns self for chaining."""
        self.steps[step.step_id] = step
        return self

    def get_execution_order(self) -> List[FlowStep]:
        """
        Return steps in dependency order (topological sort).

        Raises ValueError if circular dependency detected.
        """
        visited = set()
        in_progress = set()
        result = []

        def visit(step: FlowStep):
            if step.step_id in visited:
                return
            if step.step_id in in_progress:
                raise ValueError(f"Circular dependency detected at step: {step.step_id}")
            in_progress.add(step.step_id)
            for dep_id in step.depends_on:
                dep = self.steps.get(dep_id)
                if dep:
                    visit(dep)
            in_progress.discard(step.step_id)
            visited.add(step.step_id)
            result.append(step)

        for step in self.steps.values():
            visit(step)

        return result

    def get_ready_steps(self, completed_step_ids: set) -> List[FlowStep]:
        """Return steps whose dependencies are all satisfied."""
        ready = []
        for step in self.steps.values():
            if step.step_id in completed_step_ids:
                continue
            if all(dep in completed_step_ids for dep in step.depends_on):
                ready.append(step)
        return ready

    def to_dict(self) -> Dict[str, Any]:
        return {
            "flow_id": self.flow_id,
            "name": self.name,
            "description": self.description,
            "steps": [s.to_dict() for s in self.get_execution_order()],
        }
