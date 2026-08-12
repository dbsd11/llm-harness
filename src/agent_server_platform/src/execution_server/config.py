# Execution-server config - loaded from env (shared .env or execution_server.env).
import os
from dotenv import load_dotenv


def load_config() -> dict:
    """Load execution-server config from environment.

    Includes both server connection params and LLM configuration.
    """
    load_dotenv()
    server_id = os.getenv("SERVER_ID", "node-1")
    return {
        # Server identity and registration
        "server_id": server_id,
        "server_name": os.getenv("SERVER_NAME") or server_id,
        "max_quota": int(os.getenv("MAX_QUOTA", "4")),
        "heartbeat_interval": int(os.getenv("HEARTBEAT_INTERVAL", "5")),

        # Backend WebSocket connection
        "backend_ws_url": os.getenv("BACKEND_WS_URL", "wss://agent-socket-server.bdzz.com.cn:8765"),
        "api_key": os.getenv("WS_SERVER_API_KEY", ""),

        # LLM configuration
        "dashscope_api_key": os.getenv("DASHSCOPE_API_KEY"),
        "llm_base_url": os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        "llm_model": os.getenv("LLM_MODEL", "qwen-plus"),
        "llm_max_tokens": int(os.getenv("LLM_MAX_TOKENS", "4096")),
        "llm_enable_thinking": os.getenv("LLM_ENABLE_THINKING", "true").lower() == "true",
        "llm_timeout": int(os.getenv("LLM_TIMEOUT", "120")),
    }
