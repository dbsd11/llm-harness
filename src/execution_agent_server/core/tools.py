# Built-in tools for the execution agent server.
#
# Provides BashTool (shell execution) and AskAssistantTool (request info
# from the assistant role via the backend).
import json
import subprocess
from typing import Optional

from core.tool_registry import Tool
from core.pending_answer import pending_answers
from core import ws_protocol as P
from logger import logger

_DEFAULT_CMD_TIMEOUT = 30


def _execute_bash(command: str, timeout: int = _DEFAULT_CMD_TIMEOUT) -> str:
    """Execute a shell command and return the output."""
    try:
        result = subprocess.run(
            command, shell=True, executable="/bin/bash",
            capture_output=True, text=True, timeout=timeout,
        )
        output = result.stdout
        if result.stderr:
            output += ("\n" if output else "") + result.stderr
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        return output.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return f"[error] command timed out after {timeout}s"
    except Exception as e:
        return f"[error] {e}"


class BashTool(Tool):
    name = "run_bash"
    description = (
        "在服务器本地 shell 中执行 bash 命令。"
        "可运行任意 shell 命令（python、curl、文件操作等）。"
        "返回 stdout 和 stderr 的合并输出，以及退出码。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 bash 命令",
            },
            "timeout": {
                "type": "integer",
                "description": f"命令超时秒数，默认 {_DEFAULT_CMD_TIMEOUT}s",
            },
        },
        "required": ["command"],
    }

    def execute(self, command: str, timeout: int = _DEFAULT_CMD_TIMEOUT) -> str:
        return _execute_bash(command, timeout)


class AskAssistantTool(Tool):
    """Request supplementary information from the assistant role.

    Sends an ask_assistant_request frame to the backend, which creates a task
    for the assistant. Blocks until the assistant's response arrives or timeout.
    """

    name = "ask_assistant"
    description = (
        "向助手（assistant）请求补充信息。"
        "当你需要额外的信息、确认或反馈才能继续完成任务时，调用此工具。"
        "助手会收到你的问题并返回回答。"
        "请清晰、具体地描述你需要的信息。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "需要助手回答的具体问题或信息请求",
            },
        },
        "required": ["question"],
    }

    def __init__(self, ws_client, task_id: str, context_summary: str = ""):
        self._ws_client = ws_client
        self._task_id = task_id
        self._context_summary = context_summary

    def execute(self, question: str) -> str:
        request_id = pending_answers.register(question)
        self._ws_client.send(P.ask_assistant_request_frame(
            self._task_id, request_id, question, self._context_summary,
        ))
        logger.info(f"ask_assistant sent for task {self._task_id}, "
                    f"request_id={request_id}")
        answer, success = pending_answers.wait(request_id, timeout=300)
        if not success:
            return f"[assistant error] {answer}"
        return answer


def create_tool_registry(assistant_mode: bool = False,
                         ws_client=None,
                         task_id: str = "",
                         context_summary: str = ""):
    """Create a ToolRegistry with the standard tool set.

    Args:
        assistant_mode: If True, include ask_assistant tool.
        ws_client: WS client reference (needed for ask_assistant).
        task_id: Current task ID (needed for ask_assistant).
        context_summary: Brief context for the assistant (optional).
    """
    from core.tool_registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(BashTool())

    if assistant_mode and ws_client:
        registry.register(AskAssistantTool(ws_client, task_id, context_summary))

    return registry
