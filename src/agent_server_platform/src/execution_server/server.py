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

    logger.info(
        f"Execution server starting: id={cfg['server_id']} "
        f"name={cfg['server_name']} quota={cfg['max_quota']} "
        f"backend={cfg['backend_ws_url']}"
    )

    client = WSClient(cfg)
    runner = TaskRunner(client, cfg["max_quota"])
    client.run(runner)
