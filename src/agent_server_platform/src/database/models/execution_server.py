# ExecutionServer model - a remote execution-agent server connected via WebSocket
from datetime import datetime
from typing import Dict, Type
from .base import BaseModel


class ExecutionServer(BaseModel):
    """ExecutionServer model - registered remote execution-agent servers.

    Mirrored from live WS connections so the UI dropdown + dispatcher can read
    server presence/status without IPC to the WS-server process.
    """
    __tablename__ = "execution_servers"
    __primary_key__ = "server_id"
    __fields__ = {
        "server_id": str,        # client-supplied stable id (e.g. "node-1")
        "name": str,             # human label
        "status": str,           # 'offline' | 'idle' | 'running'
        "source": str,           # 'execution_server' | 'human_agent'
        "total_quota": int,      # locally-configured max concurrent tasks
        "running_count": int,    # current in-flight tasks
        "env_info": str,         # JSON: {"bash":true,...}
        "last_heartbeat": datetime,
        "connected": bool,       # WS connection alive
        "updated_at": datetime,
    }
    __default_order__ = "server_id ASC"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
