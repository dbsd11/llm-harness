"""
Unit tests for scenarios/flow_definition.py

Tests:
- FlowStep creation and to_dict
- FlowDefinition.add_step (chaining)
- FlowDefinition.get_execution_order (topological sort)
- FlowDefinition.get_execution_order with circular dependency (raises ValueError)
- FlowDefinition.get_ready_steps
- FlowDefinition.to_dict
"""
import pytest

from scenarios.flow_definition import FlowStep, FlowDefinition


class TestFlowStep:
    def test_basic_creation(self):
        step = FlowStep("step-1", "scheduling", "Analyze data")
        assert step.step_id == "step-1"
        assert step.agent_type == "scheduling"
        assert step.goal == "Analyze data"
        assert step.depends_on == []
        assert step.config == {}

    def test_creation_with_dependencies(self):
        step = FlowStep("step-2", "execution", "Run script",
                        depends_on=["step-1"], config={"key": "value"})
        assert step.depends_on == ["step-1"]
        assert step.config == {"key": "value"}

    def test_to_dict(self):
        step = FlowStep("step-1", "scheduling", "Analyze",
                        depends_on=["step-0"], config={"timeout": 60})
        d = step.to_dict()
        assert d == {
            "step_id": "step-1",
            "agent_type": "scheduling",
            "goal": "Analyze",
            "depends_on": ["step-0"],
            "config": {"timeout": 60},
        }


class TestFlowDefinition:
    def test_empty_flow(self):
        flow = FlowDefinition("flow-1", "Empty Flow", "No steps")
        assert flow.flow_id == "flow-1"
        assert flow.name == "Empty Flow"
        assert flow.description == "No steps"
        assert flow.steps == {}

    def test_add_step_returns_self_for_chaining(self):
        flow = FlowDefinition("flow-1", "Test Flow")
        result = flow.add_step(FlowStep("s1", "scheduling", "Step 1"))
        assert result is flow

    def test_add_multiple_steps_via_chaining(self):
        flow = (
            FlowDefinition("flow-1", "Test Flow")
            .add_step(FlowStep("s1", "scheduling", "Step 1"))
            .add_step(FlowStep("s2", "execution", "Step 2"))
            .add_step(FlowStep("s3", "scheduling", "Step 3"))
        )
        assert len(flow.steps) == 3

    def test_get_execution_order_no_dependencies(self):
        flow = FlowDefinition("flow-1", "Test")
        flow.add_step(FlowStep("s1", "scheduling", "Step 1"))
        flow.add_step(FlowStep("s2", "execution", "Step 2"))
        flow.add_step(FlowStep("s3", "scheduling", "Step 3"))

        order = flow.get_execution_order()
        step_ids = [s.step_id for s in order]
        assert set(step_ids) == {"s1", "s2", "s3"}

    def test_get_execution_order_linear_chain(self):
        flow = FlowDefinition("flow-1", "Test")
        flow.add_step(FlowStep("s1", "scheduling", "Step 1"))
        flow.add_step(FlowStep("s2", "execution", "Step 2", depends_on=["s1"]))
        flow.add_step(FlowStep("s3", "scheduling", "Step 3", depends_on=["s2"]))

        order = flow.get_execution_order()
        step_ids = [s.step_id for s in order]
        assert step_ids.index("s1") < step_ids.index("s2")
        assert step_ids.index("s2") < step_ids.index("s3")

    def test_get_execution_order_diamond_dependency(self):
        """s1 -> s2, s1 -> s3, s2+s3 -> s4"""
        flow = FlowDefinition("flow-1", "Test")
        flow.add_step(FlowStep("s1", "scheduling", "Step 1"))
        flow.add_step(FlowStep("s2", "execution", "Step 2", depends_on=["s1"]))
        flow.add_step(FlowStep("s3", "execution", "Step 3", depends_on=["s1"]))
        flow.add_step(FlowStep("s4", "scheduling", "Step 4", depends_on=["s2", "s3"]))

        order = flow.get_execution_order()
        step_ids = [s.step_id for s in order]
        assert step_ids.index("s1") < step_ids.index("s2")
        assert step_ids.index("s1") < step_ids.index("s3")
        assert step_ids.index("s2") < step_ids.index("s4")
        assert step_ids.index("s3") < step_ids.index("s4")

    def test_get_execution_order_circular_dependency_raises(self):
        flow = FlowDefinition("flow-1", "Test")
        flow.add_step(FlowStep("s1", "scheduling", "Step 1", depends_on=["s2"]))
        flow.add_step(FlowStep("s2", "execution", "Step 2", depends_on=["s1"]))

        with pytest.raises(ValueError, match="Circular dependency"):
            flow.get_execution_order()

    def test_get_execution_order_self_dependency_raises(self):
        flow = FlowDefinition("flow-1", "Test")
        flow.add_step(FlowStep("s1", "scheduling", "Step 1", depends_on=["s1"]))

        with pytest.raises(ValueError, match="Circular dependency"):
            flow.get_execution_order()

    def test_get_ready_steps_no_dependencies(self):
        flow = FlowDefinition("flow-1", "Test")
        flow.add_step(FlowStep("s1", "scheduling", "Step 1"))
        flow.add_step(FlowStep("s2", "execution", "Step 2"))

        ready = flow.get_ready_steps(set())
        assert len(ready) == 2

    def test_get_ready_steps_with_dependencies(self):
        flow = FlowDefinition("flow-1", "Test")
        flow.add_step(FlowStep("s1", "scheduling", "Step 1"))
        flow.add_step(FlowStep("s2", "execution", "Step 2", depends_on=["s1"]))
        flow.add_step(FlowStep("s3", "scheduling", "Step 3", depends_on=["s1", "s2"]))

        # Nothing completed: only s1 is ready
        ready = flow.get_ready_steps(set())
        assert len(ready) == 1
        assert ready[0].step_id == "s1"

        # s1 completed: s2 is ready
        ready = flow.get_ready_steps({"s1"})
        assert len(ready) == 1
        assert ready[0].step_id == "s2"

        # s1+s2 completed: s3 is ready
        ready = flow.get_ready_steps({"s1", "s2"})
        assert len(ready) == 1
        assert ready[0].step_id == "s3"

        # All completed: nothing ready
        ready = flow.get_ready_steps({"s1", "s2", "s3"})
        assert len(ready) == 0

    def test_get_ready_steps_excludes_completed(self):
        flow = FlowDefinition("flow-1", "Test")
        flow.add_step(FlowStep("s1", "scheduling", "Step 1"))

        ready = flow.get_ready_steps({"s1"})
        assert len(ready) == 0

    def test_to_dict(self):
        flow = FlowDefinition("flow-1", "Test Flow", "A test")
        flow.add_step(FlowStep("s1", "scheduling", "Step 1"))
        flow.add_step(FlowStep("s2", "execution", "Step 2", depends_on=["s1"]))

        d = flow.to_dict()
        assert d["flow_id"] == "flow-1"
        assert d["name"] == "Test Flow"
        assert d["description"] == "A test"
        assert len(d["steps"]) == 2
        # Steps should be in execution order
        assert d["steps"][0]["step_id"] == "s1"
        assert d["steps"][1]["step_id"] == "s2"

    def test_dependency_on_nonexistent_step_is_ignored(self):
        """If a step depends on a step_id not in the flow, it's treated as satisfied."""
        flow = FlowDefinition("flow-1", "Test")
        flow.add_step(FlowStep("s1", "scheduling", "Step 1", depends_on=["nonexistent"]))

        order = flow.get_execution_order()
        assert len(order) == 1
        assert order[0].step_id == "s1"
