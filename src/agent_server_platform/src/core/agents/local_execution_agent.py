# Execution Agent - ReAct agent with bash tool
import json
import subprocess
from typing import Dict, Any, List, Optional
from .local_base_agent import BaseAgent
from core.local_llm_client import llm_client
from core.local_event_bus import event_bus
from logger import logger

_MAX_REACT_ITERATIONS = 10
_DEFAULT_CMD_TIMEOUT = 30

BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "run_bash",
        "description": (
            "在服务器本地 shell 中执行 bash 命令。"
            "可运行任意 shell 命令（python、curl、文件操作等）。"
            "返回 stdout 和 stderr 的合并输出，以及退出码。"
        ),
        "parameters": {
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
        },
    },
}

TOOLS = [BASH_TOOL]


def _execute_bash(command: str, timeout: int = _DEFAULT_CMD_TIMEOUT) -> str:
    """Run a shell command and return its combined output + exit code."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            executable="/bin/bash",
            capture_output=True,
            text=True,
            timeout=timeout,
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


class ExecutionAgent(BaseAgent):
    """
    Execution Agent: ReAct loop with bash tool.

    The agent reasons about the task, calls bash tools when it needs to
    execute commands, observes the results, and iterates until it produces
    a final answer.
    """

    def __init__(self):
        self.config = {}
        self.role = None
        self.system_prompt = None

    def get_agent_type(self) -> str:
        return "execution"

    def initialize(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.role = config.get("role", "general assistant")
        self.system_prompt = config.get("system_prompt", "You are a helpful assistant.")
        logger.info(f"ExecutionAgent initialized with role: {self.role}")

    def run(self, task_id: str, context: Dict[str, Any]) -> Dict[str, Any]:
        event_bus.emit("task.execution_started", {"task_id": task_id})

        try:
            question = context.get("goal") or context.get("question", "")
            if not question:
                raise ValueError("No question or goal provided in context")

            upstream = context.get("upstream_outputs") or []
            logger.info(f"ExecutionAgent (ReAct) processing: {question[:100]}...")

            output = self._react_loop(task_id, question, upstream)

            event_bus.emit("task.execution_completed", {
                "task_id": task_id,
                "role": self.role,
                "response_length": len(output),
            })

            return {
                "success": True,
                "output": output,
                "role": self.role,
                "question": question,
            }

        except Exception as e:
            error_msg = str(e)
            logger.error(f"ExecutionAgent error for task {task_id}: {error_msg}")
            event_bus.emit("task.execution_failed", {
                "task_id": task_id,
                "error": error_msg,
            })
            return {
                "success": False,
                "output": "",
                "error": error_msg,
            }

    def _react_loop(self, task_id: str, question: str,
                    upstream: List[str]) -> str:
        """ReAct loop: reason -> act (tool) -> observe -> repeat."""
        system_msg = (
            f"{self.system_prompt}\n\n"
            "你可以通过 run_bash 工具在服务器 shell 中执行命令。"
            "当任务需要运行命令（python 脚本、curl、文件操作等）时，请使用该工具。"
            "观察命令输出后继续推理，直到完成任务并给出最终回答。\n\n"
            "【重要】文件输出约束：\n"
            "- /data 是持久化数据目录，任务中生成的所有文件（文本、图片、音频、视频、PDF 等多模态文件）必须保存到 /data 目录下。\n"
            "- 不要将生成的文件保存到 /tmp、/app 或其他临时目录，这些目录在容器重启后会丢失。\n"
            "- 在最终回答中，请列出所有生成文件的完整路径（如 /data/report.pdf、/data/chart.png），以便后续任务或用户可以找到它们。"
        )
        if upstream:
            system_msg += "\n\n前序任务的输出：\n" + "\n---\n".join(upstream)

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": question},
        ]

        final_text = ""

        for iteration in range(_MAX_REACT_ITERATIONS):
            if not llm_client.client:
                raise RuntimeError("LLM client not configured")

            response = llm_client.chat_with_tools(messages, TOOLS, temperature=0.3)
            if response is None:
                raise RuntimeError("LLM returned empty response")

            content = response.get("content", "") or ""
            tool_calls = response.get("tool_calls")

            # Append the assistant message to conversation history.
            assistant_msg: Dict[str, Any] = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            if not tool_calls:
                final_text = content
                logger.info(
                    f"ReAct loop ended after {iteration + 1} iteration(s) "
                    f"for task {task_id}"
                )
                break

            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    fn_args = {}

                if fn_name == "run_bash":
                    cmd = fn_args.get("command", "")
                    timeout = fn_args.get("timeout", _DEFAULT_CMD_TIMEOUT)
                    logger.info(f"ReAct executing bash: {cmd[:200]}")
                    tool_output = _execute_bash(cmd, timeout)
                    logger.info(f"ReAct bash output ({len(tool_output)} chars)")
                else:
                    tool_output = f"[error] unknown tool: {fn_name}"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_output,
                })
        else:
            final_text = content or "[max iterations reached]"
            logger.warning(
                f"ReAct loop hit max iterations ({_MAX_REACT_ITERATIONS}) "
                f"for task {task_id}"
            )

        return final_text

    def cleanup(self) -> None:
        logger.info("ExecutionAgent cleaned up")
