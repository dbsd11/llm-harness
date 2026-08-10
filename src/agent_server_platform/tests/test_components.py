"""
Unit tests for scenarios/components.py

Tests:
- ScenarioComponent (concrete subclass)
- ComponentResult creation and to_dict
- ScenarioOutput: add_result, success property, to_dict
"""
import pytest
from datetime import datetime

from scenarios.components import ScenarioComponent, ComponentResult, ScenarioOutput


class EchoComponent(ScenarioComponent):
    """Test component that echoes input."""

    def execute(self, context):
        return {"echo": context.get("input", ""), "status": "ok"}


class FailingComponent(ScenarioComponent):
    """Test component that raises."""

    def execute(self, context):
        raise RuntimeError("intentional failure")


class TestScenarioComponent:
    def test_component_attributes(self):
        comp = EchoComponent("comp-1", "Echo", config={"mode": "test"})
        assert comp.component_id == "comp-1"
        assert comp.name == "Echo"
        assert comp.config == {"mode": "test"}

    def test_get_component_type(self):
        comp = EchoComponent("comp-1", "Echo")
        assert comp.get_component_type() == "EchoComponent"

    def test_to_dict(self):
        comp = EchoComponent("comp-1", "Echo", config={"key": "val"})
        d = comp.to_dict()
        assert d["component_id"] == "comp-1"
        assert d["name"] == "Echo"
        assert d["type"] == "EchoComponent"
        assert d["config"] == {"key": "val"}

    def test_execute_returns_result(self):
        comp = EchoComponent("comp-1", "Echo")
        result = comp.execute({"input": "hello"})
        assert result == {"echo": "hello", "status": "ok"}

    def test_failing_component_raises(self):
        comp = FailingComponent("comp-2", "Failing")
        with pytest.raises(RuntimeError, match="intentional failure"):
            comp.execute({})


class TestComponentResult:
    def test_default_success_result(self):
        r = ComponentResult("comp-1")
        assert r.component_id == "comp-1"
        assert r.status == "success"
        assert r.data == {}
        assert r.error is None
        assert isinstance(r.executed_at, datetime)

    def test_failed_result(self):
        r = ComponentResult("comp-1", status="failed",
                            error="something broke")
        assert r.status == "failed"
        assert r.error == "something broke"

    def test_result_with_data(self):
        r = ComponentResult("comp-1", data={"output": "hello"})
        assert r.data == {"output": "hello"}

    def test_to_dict(self):
        r = ComponentResult("comp-1", status="success", data={"x": 1})
        d = r.to_dict()
        assert d["component_id"] == "comp-1"
        assert d["status"] == "success"
        assert d["data"] == {"x": 1}
        assert d["error"] is None
        assert "executed_at" in d

    def test_skipped_status(self):
        r = ComponentResult("comp-1", status="skipped")
        assert r.status == "skipped"


class TestScenarioOutput:
    def test_empty_output(self):
        output = ScenarioOutput("scenario-1")
        assert output.scenario_id == "scenario-1"
        assert output.component_results == {}
        # Empty output: all([]) is True
        assert output.success is True

    def test_add_result(self):
        output = ScenarioOutput("scenario-1")
        r = ComponentResult("comp-1", status="success", data={"x": 1})
        output.add_result(r)
        assert "comp-1" in output.component_results
        assert output.component_results["comp-1"] is r

    def test_success_when_all_succeed(self):
        output = ScenarioOutput("scenario-1")
        output.add_result(ComponentResult("comp-1", status="success"))
        output.add_result(ComponentResult("comp-2", status="success"))
        assert output.success is True

    def test_failure_when_any_component_fails(self):
        output = ScenarioOutput("scenario-1")
        output.add_result(ComponentResult("comp-1", status="success"))
        output.add_result(ComponentResult("comp-2", status="failed", error="oops"))
        assert output.success is False

    def test_failure_when_any_component_skipped(self):
        output = ScenarioOutput("scenario-1")
        output.add_result(ComponentResult("comp-1", status="success"))
        output.add_result(ComponentResult("comp-2", status="skipped"))
        assert output.success is False

    def test_to_dict(self):
        output = ScenarioOutput("scenario-1")
        output.add_result(ComponentResult("comp-1", status="success", data={"a": 1}))
        output.add_result(ComponentResult("comp-2", status="success", data={"b": 2}))

        d = output.to_dict()
        assert d["scenario_id"] == "scenario-1"
        assert d["success"] is True
        assert "comp-1" in d["components"]
        assert "comp-2" in d["components"]
        assert d["components"]["comp-1"]["data"] == {"a": 1}
        assert d["components"]["comp-2"]["data"] == {"b": 2}

    def test_to_dict_with_failure(self):
        output = ScenarioOutput("scenario-1")
        output.add_result(ComponentResult("comp-1", status="failed", error="broken"))

        d = output.to_dict()
        assert d["success"] is False
        assert d["components"]["comp-1"]["error"] == "broken"

    def test_later_result_overwrites(self):
        """Adding a result with the same component_id overwrites the previous one."""
        output = ScenarioOutput("scenario-1")
        output.add_result(ComponentResult("comp-1", status="failed"))
        output.add_result(ComponentResult("comp-1", status="success"))
        assert output.success is True
