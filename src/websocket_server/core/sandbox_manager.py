# Sandbox Manager - Docker container lifecycle management via SSH
import os
import time
from datetime import datetime
from typing import Dict, List, Optional

import paramiko

from logger import logger
from .sandbox_config import load_sandbox_config


class SandboxManager:
    """
    Manages execution agent server sandboxes (Docker containers) on a remote server.

    Lifecycle:
    1. Create sandbox: docker run with environment variables
    2. Track sandbox state: in-memory dict + optional DB persistence
    3. Health check: docker inspect to verify container running
    4. Destroy sandbox: docker stop + docker rm
    """

    def __init__(self):
        config = load_sandbox_config()
        self.ssh_host = config["ssh_host"]
        self.ssh_user = config["ssh_user"]
        self.ssh_password = config.get("ssh_password")
        self.ssh_key_path = config["ssh_key_path"]
        self.docker_image = config["docker_image"]
        self.default_quota = config["default_quota"]
        self.backend_ws_url = config["backend_ws_url"]
        self.api_key = config.get("api_key", "")
        self.heartbeat_interval = config["heartbeat_interval"]

        # In-memory sandbox tracking: server_id -> sandbox_info
        self._sandboxes: Dict[str, Dict] = {}

    def create_sandbox(self, config: dict) -> dict:
        """
        Create a sandbox container and start the execution agent server.

        Args:
            config: {
                "server_id": str,           # Required: unique server identifier
                "server_name": str,          # Optional: display name
                "max_quota": int,            # Optional: max concurrent tasks
                "image": str,                # Optional: Docker image (overrides default)
                "backend_ws_url": str,       # Optional: backend WS URL (overrides default)
            }

        Returns:
            {"container_id": str, "container_name": str}

        Raises:
            RuntimeError: if Docker command fails
        """
        server_id = config["server_id"]
        server_name = config.get("server_name", server_id)
        max_quota = config.get("max_quota", self.default_quota)
        image = config.get("image") or self.docker_image
        backend_ws_url = config.get("backend_ws_url") or self.backend_ws_url

        # Build container name
        container_name = f"exec-server-{server_id}"

        # Check if container already exists
        if server_id in self._sandboxes:
            logger.warning(f"Sandbox already exists for {server_id}")
            return {
                "container_id": self._sandboxes[server_id]["container_id"],
                "container_name": container_name,
            }

        # Build environment variables
        env_vars = {
            "SERVER_ID": server_id,
            "SERVER_NAME": server_name,
            "MAX_QUOTA": str(max_quota),
            "BACKEND_WS_URL": backend_ws_url,
            "HEARTBEAT_INTERVAL": str(self.heartbeat_interval),
            "EVENT_PERSIST_DISABLED": "1",
        }
        if self.api_key:
            env_vars["WS_SERVER_API_KEY"] = self.api_key

        # Build docker run command with data volume mount
        env_args = " ".join([f'-e {k}="{v}"' for k, v in env_vars.items()])
        host_data_dir = f"/agent-data-files/{server_id}"
        docker_cmd = (
            f"mkdir -p {host_data_dir} && "
            f"docker run -d "
            f"--name {container_name} "
            f"-v {host_data_dir}:/data "
            f"{env_args} "
            f"{image}"
        )

        logger.info(f"Creating sandbox: {server_id} with image {image}")

        # Execute via SSH
        try:
            stdout, stderr = self._ssh_exec(docker_cmd)
            container_id = stdout.strip()
            error = stderr.strip()

            if error or not container_id:
                raise RuntimeError(f"Docker run failed: {error or 'no container ID'}")

            # Record sandbox info
            self._sandboxes[server_id] = {
                "container_id": container_id,
                "container_name": container_name,
                "created_at": datetime.now(),
                "config": config,
                "status": "created",
            }

            logger.info(f"Sandbox created: {server_id} -> {container_id[:12]}")

            # Wait for container to start (up to 10 seconds)
            for i in range(10):
                if self.check_sandbox_health(server_id):
                    self._sandboxes[server_id]["status"] = "running"
                    logger.info(f"Sandbox {server_id} is running")
                    break
                time.sleep(1)
            else:
                logger.warning(f"Sandbox {server_id} did not start within 10s")

            return {
                "container_id": container_id,
                "container_name": container_name,
            }

        except Exception as e:
            logger.error(f"Failed to create sandbox {server_id}: {e}")
            raise

    def destroy_sandbox(self, server_id: str) -> bool:
        """
        Destroy a sandbox container.

        Args:
            server_id: Server identifier

        Returns:
            True if destroyed, False if not found
        """
        if server_id not in self._sandboxes:
            logger.warning(f"Sandbox not found: {server_id}")
            return False

        sandbox = self._sandboxes[server_id]
        container_name = sandbox["container_name"]

        logger.info(f"Destroying sandbox: {server_id} ({container_name})")

        try:
            # Stop container
            self._ssh_exec(f"docker stop {container_name}")

            # Remove container
            self._ssh_exec(f"docker rm {container_name}")

            # Clean up tracking
            del self._sandboxes[server_id]

            logger.info(f"Sandbox destroyed: {server_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to destroy sandbox {server_id}: {e}")
            # Still remove from tracking to avoid stale state
            del self._sandboxes[server_id]
            return False

    def list_sandboxes(self) -> list:
        """
        List all sandboxes (in-memory tracking).

        Returns:
            List of sandbox info dicts
        """
        return [
            {
                "server_id": server_id,
                "container_id": info["container_id"],
                "container_name": info["container_name"],
                "created_at": info["created_at"].isoformat(),
                "status": info.get("status", "unknown"),
            }
            for server_id, info in self._sandboxes.items()
        ]

    def check_sandbox_health(self, server_id: str) -> bool:
        """
        Check if a sandbox container is running.

        Args:
            server_id: Server identifier

        Returns:
            True if running, False otherwise
        """
        if server_id not in self._sandboxes:
            return False

        sandbox = self._sandboxes[server_id]
        container_name = sandbox["container_name"]

        try:
            stdout, stderr = self._ssh_exec(
                f"docker inspect -f '{{{{.State.Running}}}}' {container_name}"
            )
            status = stdout.strip()
            return status == "true"
        except Exception as e:
            logger.error(f"Health check failed for {server_id}: {e}")
            return False

    def get_sandbox_status(self, server_id: str) -> dict:
        """
        Get detailed status of a sandbox.

        Args:
            server_id: Server identifier

        Returns:
            Status dict or None if not found
        """
        if server_id not in self._sandboxes:
            return None

        sandbox = self._sandboxes[server_id]
        is_running = self.check_sandbox_health(server_id)

        return {
            "server_id": server_id,
            "container_id": sandbox["container_id"],
            "container_name": sandbox["container_name"],
            "created_at": sandbox["created_at"].isoformat(),
            "running": is_running,
            "status": "running" if is_running else "stopped",
        }

    def cleanup_dead_sandboxes(self) -> list:
        """
        Find and remove dead sandboxes.

        Returns:
            List of server_ids that were cleaned up
        """
        dead_servers = []

        for server_id in list(self._sandboxes.keys()):
            if not self.check_sandbox_health(server_id):
                logger.warning(f"Cleaning up dead sandbox: {server_id}")
                self.destroy_sandbox(server_id)
                dead_servers.append(server_id)

        return dead_servers

    def _ssh_exec(self, command: str) -> tuple:
        """
        Execute a command on the remote server via SSH.

        Args:
            command: Command to execute

        Returns:
            (stdout, stderr) tuple
        """
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            # Try password authentication first, then fall back to key
            if self.ssh_password:
                ssh.connect(
                    hostname=self.ssh_host,
                    username=self.ssh_user,
                    password=self.ssh_password,
                    timeout=10,
                )
            else:
                key_path = os.path.expanduser(self.ssh_key_path)
                ssh.connect(
                    hostname=self.ssh_host,
                    username=self.ssh_user,
                    key_filename=key_path,
                    timeout=10,
                )

            # Use sudo for docker commands
            if command.startswith("docker"):
                command = f"sudo -s bash -c '{command}'"

            stdin, stdout, stderr = ssh.exec_command(command, timeout=30)
            out = stdout.read().decode()
            err = stderr.read().decode()

            return out, err

        finally:
            ssh.close()
