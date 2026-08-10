# Execution-server entry: load config, wire WS client + task runner, run.
import os

from logger import logger
from .config import load_config
from .ws_client import WSClient
from .task_runner import TaskRunner


def main() -> None:
    cfg = load_config()

    # The exec-server has no backend DB access; it reports events over WS.
    # Disable the DB-backed event_bus persistence (see core/event_bus.py).
    os.environ.setdefault("EVENT_PERSIST_DISABLED", "1")

    # Propagate LLM config to env so LLMClient (which reads os.getenv) picks them up.
    if cfg.get("dashscope_api_key"):
        os.environ.setdefault("DASHSCOPE_API_KEY", cfg["dashscope_api_key"])
    os.environ.setdefault("LLM_BASE_URL", cfg["llm_base_url"])
    os.environ.setdefault("LLM_MODEL", cfg["llm_model"])
    os.environ.setdefault("LLM_MAX_TOKENS", str(cfg["llm_max_tokens"]))
    os.environ.setdefault("LLM_ENABLE_THINKING", str(cfg["llm_enable_thinking"]).lower())
    os.environ.setdefault("LLM_TIMEOUT", str(cfg["llm_timeout"]))

    api_key_mask = (cfg["dashscope_api_key"][:8] + "***") if cfg.get("dashscope_api_key") else "NOT_SET"
    logger.info(
        f"Execution server starting: id={cfg['server_id']} "
        f"name={cfg['server_name']} quota={cfg['max_quota']} "
        f"backend={cfg['backend_ws_url']}"
    )
    logger.info(
        f"LLM config: model={cfg['llm_model']} base_url={cfg['llm_base_url']} "
        f"max_tokens={cfg['llm_max_tokens']} thinking={cfg['llm_enable_thinking']} "
        f"timeout={cfg['llm_timeout']}s api_key={api_key_mask}"
    )

    client = WSClient(cfg)
    runner = TaskRunner(client, cfg["max_quota"])
    client.run(runner)
