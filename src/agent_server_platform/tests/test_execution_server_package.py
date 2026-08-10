"""
Unit tests for the standalone execution_server package.

- env_probe.probe_env: returns commands (incl. python) + host info
  (hostname / ip / os) in a nested structure
- TaskRunner: emits the agent_created / task_started / task_result telemetry
  frames and tracks running_count 0 -> 1 -> 0 (ExecutionAgent stubbed to
  avoid a real LLM call)
"""
import json
import threading

import pytest

from execution_server.env_probe import probe_env, COMMANDS, HOST_FIELDS
from execution_server.env_probe import _resolve_hostname
from execution_server.task_runner import TaskRunner
from core import ws_protocol as P


# --- env_probe --------------------------------------------------------------

class TestEnvProbe:
    def test_has_commands_and_host_sections(self):
        env = probe_env()
        assert set(env.keys()) == {"commands", "host"}

    def test_reports_all_required_commands(self):
        cmds = probe_env()["commands"]
        for cmd in ["bash", "sh", "claude", "codex", "qwen",
                    "curl", "wget", "ls", "mkdir", "cat", "sed", "python"]:
            assert cmd in cmds, f"missing {cmd}"
        assert set(cmds.keys()) == set(COMMANDS)
        assert all(isinstance(v, bool) for v in cmds.values())

    def test_reports_host_info(self):
        host = probe_env()["host"]
        for field in HOST_FIELDS:
            assert field in host, f"missing host field {field}"
        assert isinstance(host["hostname"], str) and host["hostname"]
        assert isinstance(host["ip"], str) and host["ip"]
        assert isinstance(host["os"], str) and host["os"]


class TestResolveHostname:
    """The POSIX hostname can be a useless 'localhost' placeholder; the real
    machine name must be resolved from a platform-specific source instead."""

    def test_uses_gethostname_when_it_is_a_real_name(self, monkeypatch):
        monkeypatch.setattr("socket.gethostname", lambda: "real-host")
        # Even if the platform-specific fallback would return something,
        # a real gethostname() wins.
        monkeypatch.setattr("subprocess.check_output",
                            lambda *a, **k: pytest.fail("should not call subprocess"))
        assert _resolve_hostname() == "real-host"

    def test_falls_back_to_platform_name_when_posix_is_localhost(self, monkeypatch):
        # POSIX hostname is the useless placeholder.
        monkeypatch.setattr("socket.gethostname", lambda: "localhost")
        # Simulate macOS scutil returning the real Bonjour name.
        monkeypatch.setattr("platform.system", lambda: "Darwin")

        def _fake_check_output(cmd, *a, **k):
            assert "scutil" in cmd[0]
            return b"MacBookdeMacBook-Air\n"

        monkeypatch.setattr("subprocess.check_output", _fake_check_output)
        assert _resolve_hostname() == "MacBookdeMacBook-Air"

    def test_never_returns_the_localhost_placeholder(self, monkeypatch):
        # Every source returns localhost / empty -> still must not return
        # the bare 'localhost' string; fall back to a labeled unknown.
        monkeypatch.setattr("socket.gethostname", lambda: "localhost")
        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.setattr("subprocess.check_output",
                            lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
        monkeypatch.setattr("platform.node", lambda: "")
        # /etc/hostname absent
        import builtins
        real_open = builtins.open
        monkeypatch.setattr(builtins, "open",
                            lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError))
        result = _resolve_hostname()
        assert result and result != "localhost"




# --- TaskRunner -------------------------------------------------------------

class _FakeWSClient:
    """Records every frame the runner sends over the WS link."""
    def __init__(self):
        self.sent = []

    def send(self, data):
        self.sent.append(data)


@pytest.fixture
def stub_execution_agent(monkeypatch):
    """Replace ExecutionAgent.run with a deterministic stub (no LLM)."""
    from core.agents import execution_agent

    def _fake_run(self, task_id, context):
        return {"success": True, "output": "42", "role": self.role,
                "question": context.get("question", "")}

    monkeypatch.setattr(execution_agent.ExecutionAgent, "run", _fake_run)
    return execution_agent.ExecutionAgent


class TestTaskRunner:
    def test_emits_telemetry_frames_and_result(self, stub_execution_agent):
        ws = _FakeWSClient()
        runner = TaskRunner(ws, max_quota=2)

        runner.submit("t1", "p1", "compute",
                      {"role": "数学家", "system_prompt": "s", "question": "?"})

        # Block until the worker pool has flushed the task.
        runner.executor.shutdown(wait=True)

        types = [P.parse_frame(f)["type"] for f in ws.sent]
        assert types[0] == P.TYPE_TASK_EVENT
        assert P.parse_frame(ws.sent[0])["payload"]["event"] == P.EVENT_AGENT_CREATED
        assert types[1] == P.TYPE_TASK_EVENT
        assert P.parse_frame(ws.sent[1])["payload"]["event"] == P.EVENT_TASK_STARTED
        assert types[2] == P.TYPE_TASK_RESULT

        result_frame = P.parse_frame(ws.sent[2])
        assert result_frame["payload"]["task_id"] == "t1"
        assert result_frame["payload"]["success"] is True
        assert result_frame["payload"]["result"]["output"] == "42"

    def test_running_count_returns_to_zero(self, stub_execution_agent):
        ws = _FakeWSClient()
        runner = TaskRunner(ws, max_quota=2)
        assert runner.running_count == 0

        runner.submit("t1", "p", "g", {"role": "r", "system_prompt": "s"})
        runner.executor.shutdown(wait=True)
        assert runner.running_count == 0

    def test_failure_still_sends_result(self, monkeypatch):
        from core.agents import execution_agent

        def _boom(self, task_id, context):
            raise RuntimeError("agent crashed")
        monkeypatch.setattr(execution_agent.ExecutionAgent, "run", _boom)

        ws = _FakeWSClient()
        runner = TaskRunner(ws, max_quota=1)
        runner.submit("t1", "p", "g", {"role": "r", "system_prompt": "s"})
        runner.executor.shutdown(wait=True)

        result_frame = P.parse_frame(ws.sent[-1])
        assert result_frame["type"] == P.TYPE_TASK_RESULT
        assert result_frame["payload"]["success"] is False
        assert "agent crashed" in result_frame["payload"]["result"]["error"]
