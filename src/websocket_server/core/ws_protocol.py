# WebSocket protocol - frame envelope + (de)serialize helpers for the
# backend <-> execution-agent-server link.
import json
from typing import Any, Dict, Optional

# Frame types: execution-server -> backend
TYPE_REGISTER = "register"        # one-time on connect
TYPE_STATUS = "status"            # periodic heartbeat (status + env + counts)
TYPE_TASK_EVENT = "task_event"    # execution_agent_created / task_started
TYPE_TASK_RESULT = "task_result"  # terminal reply for a task

# Frame types: backend -> execution-server
TYPE_TASK = "task"                # dispatch a task to be executed
TYPE_ACK = "ack"                  # acknowledge register / task receipt
TYPE_EVENT = "event"              # backend -> subscriber broadcast

# task_event sub-events
EVENT_AGENT_CREATED = "execution_agent_created"
EVENT_TASK_STARTED = "task_started"

# server status values
STATUS_OFFLINE = "offline"
STATUS_IDLE = "idle"
STATUS_RUNNING = "running"


def make_frame(frame_type: str, payload: Dict[str, Any],
               task_id: Optional[str] = None) -> str:
    """Serialize a frame to a JSON string for the wire."""
    return json.dumps({
        "type": frame_type,
        "task_id": task_id,
        "payload": payload,
    }, ensure_ascii=False)


def parse_frame(raw: str) -> Dict[str, Any]:
    """Deserialize a frame from a JSON string.

    Returns {"type", "task_id", "payload"} or raises ValueError.
    """
    data = json.loads(raw)
    if not isinstance(data, dict) or "type" not in data:
        raise ValueError(f"Invalid frame: missing 'type' — {raw[:120]}")
    data.setdefault("task_id", None)
    data.setdefault("payload", {})
    return data


# --- convenience constructors -------------------------------------------------

def register_frame(server_id: str, name: str, total_quota: int,
                   env_info: Dict[str, bool], source: str = "execution_server") -> str:
    # `source` annotates who registered so the registry/UI can distinguish
    # execution-agent servers from human-agent browsers. Default keeps legacy
    # exec-server callers correct without code changes.
    return make_frame(TYPE_REGISTER, {
        "server_id": server_id,
        "name": name,
        "total_quota": int(total_quota),
        "env_info": env_info,
        "source": source,
    })


def status_frame(status: str, total_quota: int, running_count: int,
                 env_info: Dict[str, bool]) -> str:
    return make_frame(TYPE_STATUS, {
        "status": status,
        "total_quota": int(total_quota),
        "running_count": int(running_count),
        "env_info": env_info,
    })


def task_event_frame(task_id: str, event: str, **extra) -> str:
    payload = {"event": event}
    payload.update(extra)
    return make_frame(TYPE_TASK_EVENT, payload, task_id=task_id)


def task_result_frame(task_id: str, success: bool, result: Dict[str, Any]) -> str:
    return make_frame(TYPE_TASK_RESULT, {
        "task_id": task_id,
        "success": success,
        "result": result,
    }, task_id=task_id)


def task_frame(task_id: str, parent_task_id: str, goal: str,
               context: Dict[str, Any]) -> str:
    return make_frame(TYPE_TASK, {
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "goal": goal,
        "context": context,
    }, task_id=task_id)


def ack_frame(ok: bool = True, error: Optional[str] = None,
              task_id: Optional[str] = None) -> str:
    payload = {"ok": ok}
    if error:
        payload["error"] = error
    return make_frame(TYPE_ACK, payload, task_id=task_id)


if __name__ == "__main__":
    # Self-check: every constructor round-trips through parse_frame.
    for raw in (
        register_frame("node-1", "Node 1", 4, {"bash": True, "sed": False}),
        status_frame(STATUS_RUNNING, 4, 2, {"bash": True}),
        task_event_frame("t1", EVENT_AGENT_CREATED, role="数学家"),
        task_result_frame("t1", True, {"output": "42"}),
        task_frame("t1", "p1", "calc", {"role": "数学家", "system_prompt": "x"}),
        ack_frame(True, task_id="t1"),
    ):
        f = parse_frame(raw)
        assert "type" in f and "payload" in f, f
        again = make_frame(f["type"], f["payload"], f["task_id"])
        assert parse_frame(again)["payload"] == f["payload"]
    print("ws_protocol self-check OK")
