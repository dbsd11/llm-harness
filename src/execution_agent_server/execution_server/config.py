# Execution-server config - loaded from env (shared .env or execution_server.env).
import os
from dotenv import load_dotenv


def load_config() -> dict:
    """Load execution-server config from environment.

    Reuses the backend LLM env (DASHSCOPE_API_KEY, LLM_BASE_URL, ...) so the
    exec-server can call the same model the backend uses.
    """
    load_dotenv()
    server_id = os.getenv("SERVER_ID", "node-1")
    return {
        "backend_ws_url": os.getenv("BACKEND_WS_URL", "ws://127.0.0.1:8765"),
        "server_id": server_id,
        "server_name": os.getenv("SERVER_NAME") or server_id,
        "max_quota": int(os.getenv("MAX_QUOTA", "4")),
        "heartbeat_interval": int(os.getenv("HEARTBEAT_INTERVAL", "5")),
    }
