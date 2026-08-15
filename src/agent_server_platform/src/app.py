# Main entry point for agent-server-platform
import os
import sys

# Add src to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env BEFORE any other imports — watchdog/etc create ConnectionManager at import time
from dotenv import load_dotenv
load_dotenv()

import argparse
import signal
import multiprocessing

from database import init_database, close_database
from logger import logger
from core.local_tenant import init_tenant
from core.local_watchdog import watchdog


def signal_handler(signum, frame):
    """Handle shutdown signals"""
    logger.info(f"Received signal {signum}, shutting down...")
    watchdog.stop()
    close_database()
    sys.exit(0)


def start_gradio(port: int, host: str):
    """Start Gradio server.

    Gradio manages its own event loop (uvicorn default), completely isolated
    from global_loop_util. HTTP requests, queue processing, and session creation
    all run on Gradio's own loop — no contention with background async tasks
    (WS subscriber, agent execution) which run on worker loops.

    Reference: /Users/macbook/src/cnipy-app/app.py
    """
    from contextlib import asynccontextmanager
    from route.router import make_gr_route

    demo = make_gr_route()

    # Start WS event subscriber on a worker loop, isolated from Gradio
    from ws_event_subscriber import get_ws_subscriber
    get_ws_subscriber().start()

    # Start HumanAgentClient — connects to remote WS server using the same
    # protocol as execution-agent servers, persists incoming task frames to
    # the local human_tasks table, and sends task_result frames when a human
    # worker submits through the Gradio UI.
    from core.human_agent_client import get_human_agent_client
    get_human_agent_client().start()

    # ponytail: mount_static must run after App.create_app sets demo.app.
    # Use a lifespan handler — Gradio chains it with its own via app_kwargs.
    @asynccontextmanager
    async def _lifespan(app):
        from pages.human_agent import mount_static
        mount_static(demo)
        yield

    logger.info(f"Starting Gradio on {host}:{port}")
    demo.launch(
        server_name=host, server_port=port, share=False,
        app_kwargs={"lifespan": _lifespan},
    )


def start_flask(port: int, host: str):
    """Start Flask API server (internal only — not exposed to public internet)"""
    from api.flask_app import create_app

    # 只监听内网地址，Flask API 不对外暴露
    internal_host = os.getenv("FLASK_HOST", "127.0.0.1")
    logger.info(f"Starting Flask on {internal_host}:{port} (internal only)")
    app = create_app()
    app.run(host=internal_host, port=port, debug=False)


def main():
    """Main entry point"""
    # Parse arguments
    parser = argparse.ArgumentParser(description="Agent Server Platform")
    parser.add_argument("--gradio-only", action="store_true", help="Start only Gradio server")
    parser.add_argument("--flask-only", action="store_true", help="Start only Flask server")
    parser.add_argument("--all", action="store_true", help="Start Gradio and Flask servers")
    parser.add_argument("--gradio-port", type=int, default=int(os.getenv("GRADIO_PORT", 8080)))
    parser.add_argument("--flask-port", type=int, default=int(os.getenv("FLASK_PORT", 5000)))
    parser.add_argument("--host", type=str, default=os.getenv("HOST", "0.0.0.0"))

    args = parser.parse_args()

    # Default to --all if no option specified
    if not (args.gradio_only or args.flask_only or args.all):
        args.all = True

    # Initialize tenant context from JWT before database (migration needs tenant_id)
    init_tenant()

    # Initialize database
    logger.info("Initializing database...")
    init_database()

    # Reclaim orphans from a prior crash/restart: mark in-flight scenarios +
    # tasks failed so nothing stays "running" forever (no live runner exists
    # for them after a restart).
    from scenarios.local_scenario_manager import recover_orphans_on_startup
    recover_orphans_on_startup()

    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start watchdog
    logger.info("Starting watchdog...")
    watchdog.start()

    try:
        if args.gradio_only:
            start_gradio(args.gradio_port, args.host)
        elif args.flask_only:
            start_flask(args.flask_port, args.host)
        elif args.all:
            # Start servers in separate processes
            # ponytail: WS server removed — now runs independently on remote server
            gradio_proc = multiprocessing.Process(
                target=start_gradio,
                args=(args.gradio_port, args.host)
            )
            flask_proc = multiprocessing.Process(
                target=start_flask,
                args=(args.flask_port, args.host)
            )

            gradio_proc.start()
            flask_proc.start()

            logger.info(f"Gradio server: http://{args.host}:{args.gradio_port}")
            logger.info(f"Flask API server: http://{args.host}:{args.flask_port}")
            logger.info("WS server: running independently on remote server")

            # Wait for processes
            gradio_proc.join()
            flask_proc.join()

    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        watchdog.stop()
        close_database()


if __name__ == "__main__":
    main()
