"""HTTP API client — all platform -> ws_server communication through this module."""

import os
import json
from typing import Any, Dict, List, Optional

import requests
import urllib3
from logger import logger

# 自签名证书：跳过 HTTPS 校验（与各 wss 客户端一致）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class APIClient:
    """Thin HTTP client wrapping ws_server's REST API."""

    def __init__(self):
        self.base_url = os.getenv("WS_SERVER_API_URL", "https://agent-socket-server.bdzz.com.cn:8765")

    def _get(self, path: str, params: Dict[str, Any] = None) -> dict:
        try:
            resp = requests.get(f"{self.base_url}{path}", params=params, timeout=10, verify=False)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"API GET {path} failed: {e}")
            return {"success": False, "error": str(e)}

    def _post(self, path: str, data: Dict[str, Any] = None,
              timeout: int = 180) -> dict:
        try:
            resp = requests.post(f"{self.base_url}{path}", json=data or {},
                                 timeout=timeout, verify=False)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"API POST {path} failed: {e}")
            return {"success": False, "error": str(e)}

    def _delete(self, path: str) -> dict:
        try:
            resp = requests.delete(f"{self.base_url}{path}", timeout=10, verify=False)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"API DELETE {path} failed: {e}")
            return {"success": False, "error": str(e)}

    # ── scenarios ─────────────────────────────────────────────────────────

    def create_scenario(self, scenario_type: str, name: str,
                        description: str = "", config: dict = None,
                        created_by: int = None) -> dict:
        return self._post("/api/scenarios", {
            "scenario_type": scenario_type, "name": name,
            "description": description, "config": config or {},
            "created_by": created_by,
        })

    def list_scenarios(self, state: str = None, limit: int = 100) -> dict:
        params = {"limit": limit}
        if state:
            params["state"] = state
        return self._get("/api/scenarios", params)

    def get_scenario(self, scenario_id: str) -> dict:
        return self._get(f"/api/scenarios/{scenario_id}")

    def start_scenario(self, scenario_id: str) -> dict:
        return self._post(f"/api/scenarios/{scenario_id}/start")

    def stop_scenario(self, scenario_id: str) -> dict:
        return self._post(f"/api/scenarios/{scenario_id}/stop")

    def delete_scenario(self, scenario_id: str) -> dict:
        return self._delete(f"/api/scenarios/{scenario_id}")

    def get_scenario_messages(self, scenario_id: str) -> dict:
        """Get message history timeline for a scenario."""
        return self._get(f"/api/scenarios/{scenario_id}/messages")

    # ── tasks ─────────────────────────────────────────────────────────────

    def list_tasks(self, scenario_id: str = None, state: str = None,
                   limit: int = 100) -> dict:
        params = {"limit": limit}
        if scenario_id:
            params["scenario_id"] = scenario_id
        if state:
            params["state"] = state
        return self._get("/api/tasks", params)

    def get_task(self, task_id: str) -> dict:
        return self._get(f"/api/tasks/{task_id}")

    def cancel_task(self, task_id: str) -> dict:
        return self._delete(f"/api/tasks/{task_id}")

    def accept_task(self, task_id: str, passed: bool = True,
                    feedback: str = "") -> dict:
        return self._post(f"/api/tasks/{task_id}/accept", {
            "passed": passed, "feedback": feedback,
        })

    # ── chat / scene assistant ────────────────────────────────────────────

    def chat(self, message: str, session_id: str = "default",
             history: List[Dict[str, str]] = None,
             scene_id: str = None,
             pending_scene: dict = None) -> dict:
        return self._post("/api/chat", {
            "message": message,
            "session_id": session_id,
            "history": history or [],
            "scene_id": scene_id,
            "pending_scene": pending_scene,
        })

    def chat_save(self, scene_spec: dict, session_id: str = "default",
                  scene_id: str = None) -> dict:
        return self._post("/api/chat/save", {
            "scene_spec": scene_spec,
            "session_id": session_id,
            "scene_id": scene_id,
        })

    def chat_preview(self, session_id: str = "default",
                     spec: str = None) -> dict:
        params = {"session_id": session_id}
        if spec:
            params["spec"] = spec
        return self._get("/api/chat/preview", params)

    def chat_load_scene(self, scene_id: str) -> dict:
        return self._get("/api/chat/load-scene", {"scene_id": scene_id})

    def chat_scene_index(self) -> dict:
        return self._get("/api/chat/scene-index")

    # ── events ────────────────────────────────────────────────────────────

    def list_events(self, event_type: str = None, trace_id: str = None,
                    limit: int = 100) -> dict:
        params = {"limit": limit}
        if event_type:
            params["event_type"] = event_type
        if trace_id:
            params["trace_id"] = trace_id
        return self._get("/api/events", params)

    def create_event(self, event_type: str, data: dict = None,
                     trace_id: str = "", metadata: dict = None) -> dict:
        return self._post("/api/events", {
            "event_type": event_type,
            "data": data or {},
            "trace_id": trace_id,
            "metadata": metadata or {},
        })

    # ── agents ────────────────────────────────────────────────────────────

    def list_agents(self, agent_type: str = None, status: str = None,
                    limit: int = 200) -> dict:
        params = {"limit": limit}
        if agent_type:
            params["type"] = agent_type
        if status:
            params["status"] = status
        return self._get("/api/agents", params)

    def get_agent(self, agent_id: str) -> dict:
        return self._get(f"/api/agents/{agent_id}")

    # ── servers ───────────────────────────────────────────────────────────

    def list_servers(self) -> dict:
        return self._get("/api/servers")


# Global singleton
api_client = APIClient()