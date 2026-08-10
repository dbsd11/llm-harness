# Sandbox - task execution isolation
import subprocess
import tempfile
import os
import shutil
from typing import Dict, Any
from logger import logger


class Sandbox:
    """
    Task execution sandbox.

    Evolution path:
    - Phase 1: subprocess (simple, fast, limited isolation) - current
    - Phase 2: Docker containers (strong isolation, resource limits)
    - Phase 3: Kubernetes pods (distributed, scalable)
    """

    def __init__(self):
        self.temp_dir = None
        self.process = None
        self.timeout = 300  # Default 5 minutes
        self.max_output_size = 1048576  # 1MB

    def initialize(self, config: Dict[str, Any] = None) -> None:
        """Create isolated environment"""
        config = config or {}
        self.timeout = config.get("timeout", 300)
        self.max_output_size = config.get("max_output_size", 1048576)

        self.temp_dir = tempfile.mkdtemp(prefix="agent_sandbox_")
        logger.info(f"Sandbox initialized: {self.temp_dir}")

    def execute(self, task_id: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute task in sandbox.

        Args:
            task_id: Task identifier
            context: Task context with 'script' key containing Python code

        Returns:
            Dict with success, stdout, stderr, returncode
        """
        if not self.temp_dir:
            self.initialize()

        script = context.get("script")
        if not script:
            raise ValueError("No script provided in context")

        script_path = os.path.join(self.temp_dir, "task.py")
        with open(script_path, "w") as f:
            f.write(script)

        try:
            result = subprocess.run(
                ["python", script_path],
                cwd=self.temp_dir,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )

            # Truncate output if too large
            stdout = result.stdout[:self.max_output_size] if result.stdout else ""
            stderr = result.stderr[:self.max_output_size] if result.stderr else ""

            return {
                "success": result.returncode == 0,
                "stdout": stdout,
                "stderr": stderr,
                "returncode": result.returncode,
            }

        except subprocess.TimeoutExpired:
            logger.error(f"Task {task_id} timed out in sandbox after {self.timeout}s")
            raise
        except Exception as e:
            logger.error(f"Sandbox execution error: {e}")
            raise

    def execute_command(self, task_id: str, command: str) -> Dict[str, Any]:
        """
        Execute shell command in sandbox.

        Args:
            task_id: Task identifier
            command: Shell command to execute

        Returns:
            Dict with success, stdout, stderr, returncode
        """
        if not self.temp_dir:
            self.initialize()

        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=self.temp_dir,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )

            stdout = result.stdout[:self.max_output_size] if result.stdout else ""
            stderr = result.stderr[:self.max_output_size] if result.stderr else ""

            return {
                "success": result.returncode == 0,
                "stdout": stdout,
                "stderr": stderr,
                "returncode": result.returncode,
            }

        except subprocess.TimeoutExpired:
            logger.error(f"Task {task_id} command timed out after {self.timeout}s")
            raise
        except Exception as e:
            logger.error(f"Sandbox command error: {e}")
            raise

    def cleanup(self) -> None:
        """Cleanup sandbox resources"""
        if self.temp_dir and os.path.exists(self.temp_dir):
            try:
                shutil.rmtree(self.temp_dir, ignore_errors=True)
                logger.info(f"Sandbox cleaned up: {self.temp_dir}")
            except Exception as e:
                logger.error(f"Sandbox cleanup error: {e}")
            finally:
                self.temp_dir = None

    def __enter__(self):
        """Context manager entry"""
        self.initialize()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.cleanup()
        return False
