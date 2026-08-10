"""
Unit / integration tests for core/sandbox.py

Tests:
- Sandbox initialization and cleanup
- execute() with Python script
- execute_command() with shell commands
- Timeout handling
- Error handling (bad script, missing script)
- Context manager protocol
- Output truncation
- Working directory isolation
"""
import os
import pytest
import subprocess

from core.sandbox import Sandbox


class TestSandboxLifecycle:
    def test_initialize_creates_temp_dir(self):
        sb = Sandbox()
        sb.initialize()
        assert sb.temp_dir is not None
        assert os.path.isdir(sb.temp_dir)
        sb.cleanup()

    def test_cleanup_removes_temp_dir(self):
        sb = Sandbox()
        sb.initialize()
        temp_dir = sb.temp_dir
        sb.cleanup()
        assert not os.path.exists(temp_dir)
        assert sb.temp_dir is None

    def test_cleanup_idempotent(self):
        sb = Sandbox()
        sb.initialize()
        sb.cleanup()
        sb.cleanup()  # second cleanup should not raise

    def test_initialize_with_config(self):
        sb = Sandbox()
        sb.initialize({"timeout": 10, "max_output_size": 512})
        assert sb.timeout == 10
        assert sb.max_output_size == 512
        sb.cleanup()

    def test_initialize_default_config(self):
        sb = Sandbox()
        sb.initialize()
        assert sb.timeout == 300
        assert sb.max_output_size == 1048576
        sb.cleanup()


class TestSandboxContextManager:
    def test_context_manager(self):
        with Sandbox() as sb:
            assert sb.temp_dir is not None
            assert os.path.isdir(sb.temp_dir)
            temp_dir = sb.temp_dir

        assert not os.path.exists(temp_dir)

    def test_context_manager_cleanup_on_error(self):
        try:
            with Sandbox() as sb:
                temp_dir = sb.temp_dir
                raise ValueError("test error")
        except ValueError:
            pass

        assert not os.path.exists(temp_dir)


class TestSandboxExecute:
    def test_execute_simple_script(self, sandbox):
        result = sandbox.execute("task-1", {"script": "print('hello world')"})
        assert result["success"] is True
        assert "hello world" in result["stdout"]
        assert result["returncode"] == 0

    def test_execute_script_with_error(self, sandbox):
        result = sandbox.execute("task-2", {
            "script": "import sys; print('err', file=sys.stderr); sys.exit(1)"
        })
        assert result["success"] is False
        assert result["returncode"] == 1

    def test_execute_raises_on_missing_script(self, sandbox):
        with pytest.raises(ValueError, match="No script"):
            sandbox.execute("task-3", {})

    def test_execute_script_with_computation(self, sandbox):
        script = """
x = 2 + 3
print(f"result={x}")
"""
        result = sandbox.execute("task-4", {"script": script})
        assert result["success"] is True
        assert "result=5" in result["stdout"]

    def test_execute_timeout(self):
        sb = Sandbox()
        sb.initialize({"timeout": 1})
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                sb.execute("task-5", {"script": "import time; time.sleep(10)"})
        finally:
            sb.cleanup()


class TestSandboxExecuteCommand:
    def test_execute_echo_command(self, sandbox):
        result = sandbox.execute_command("cmd-1", "echo 'hello shell'")
        assert result["success"] is True
        assert "hello shell" in result["stdout"]
        assert result["returncode"] == 0

    def test_execute_failing_command(self, sandbox):
        result = sandbox.execute_command("cmd-2", "exit 1")
        assert result["success"] is False
        assert result["returncode"] == 1

    def test_execute_command_timeout(self):
        sb = Sandbox()
        sb.initialize({"timeout": 1})
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                sb.execute_command("cmd-3", "sleep 10")
        finally:
            sb.cleanup()


class TestSandboxIsolation:
    def test_working_directory_is_temp_dir(self, sandbox):
        script = "import os; print(os.getcwd())"
        result = sandbox.execute("iso-1", {"script": script})
        assert sandbox.temp_dir in result["stdout"]

    def test_file_created_in_sandbox(self, sandbox):
        sandbox.execute_command("iso-2", "touch test_file.txt")
        assert os.path.exists(os.path.join(sandbox.temp_dir, "test_file.txt"))

    def test_env_isolation(self, sandbox):
        """Sandbox subprocess has its own env; host env is unaffected."""
        script = """
import os
os.environ['SANDBOX_TEST'] = 'from_sandbox'
print(os.environ.get('SANDBOX_TEST', ''))
"""
        result = sandbox.execute("iso-3", {"script": script})
        assert "from_sandbox" in result["stdout"]
        assert os.environ.get("SANDBOX_TEST") != "from_sandbox"
