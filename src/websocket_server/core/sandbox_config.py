# Sandbox configuration - loaded from environment
import os
from dotenv import load_dotenv


def load_sandbox_config() -> dict:
    """Load sandbox configuration from environment variables.

    Returns:
        dict with SSH, Docker, and backend configuration
    """
    load_dotenv()

    return {
        # SSH configuration for remote server
        "ssh_host": os.getenv("SANDBOX_SSH_HOST", "agent-socket-server.bdzz.com.cn"),
        "ssh_user": os.getenv("SANDBOX_SSH_USER", "ubuntu"),
        "ssh_password": os.getenv("SANDBOX_SSH_PASSWORD"),  # Password authentication
        "ssh_key_path": os.getenv("SANDBOX_SSH_KEY_PATH", "~/.ssh/id_rsa"),

        # Docker configuration
        "docker_image": os.getenv("SANDBOX_DOCKER_IMAGE", "execution-agent-server:latest"),
        "default_quota": int(os.getenv("SANDBOX_DEFAULT_QUOTA", "4")),

        # Backend configuration (for exec-server to connect back)
        "backend_ws_url": os.getenv("SANDBOX_BACKEND_WS_URL", "ws://localhost:8765"),
        "api_key": os.getenv("WS_SERVER_API_KEY", ""),
        "heartbeat_interval": int(os.getenv("SANDBOX_HEARTBEAT_INTERVAL", "5")),
    }
