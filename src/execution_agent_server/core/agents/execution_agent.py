# Execution Agent - ReAct agent with bash tool
import json
import subprocess
from typing import Dict, Any, List, Optional
from .base_agent import BaseAgent
from core.llm_client import llm_client
from core.event_bus import event_bus
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
            server_id = context.get("server_id", "")
            logger.info(f"ExecutionAgent (ReAct) processing: {question[:100]}...")

            output = self._react_loop(task_id, question, upstream, server_id=server_id)

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
                    upstream: List[str], server_id: str = "") -> str:
        """ReAct loop: reason -> act (tool) -> observe -> repeat."""
        system_msg = (
            f"{self.system_prompt}\n\n"
            "你可以通过 run_bash 工具在服务器 shell 中执行命令。\n\n"
            "【工作原则】\n"
            "1. 明确目标：理解任务要求，制定简洁的执行计划\n"
            "2. 高效执行：只运行必要的命令，避免重复探索\n"
            "3. 及时总结：获取到足够信息后，立即生成最终答案，不要继续收集数据\n"
            "4. 完整回答：最终答案应完整回应任务要求，包含所有必要信息\n\n"
            "【重要】文件输出约束：\n"
            "- /data 是持久化数据目录，任务中生成的所有文件必须保存到 /data 目录下\n"
            "- 不要将生成的文件保存到 /tmp、/app 或其他临时目录\n"
            "- 在最终回答中列出所有生成文件的完整路径\n\n"
            "【注意】你最多有 10 次工具调用机会，请高效利用。当收集到足够信息时，直接给出完整答案，不要再调用工具。"
        )
        if server_id:
            system_msg += f"\n\n当前执行服务器 ID：`{server_id}`"
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
            logger.warning(
                f"ReAct loop hit max iterations ({_MAX_REACT_ITERATIONS}) "
                f"for task {task_id}, synthesizing final answer from context"
            )
            final_text = self._synthesize_final_answer(messages, task_id)

        return final_text

    def _synthesize_final_answer(self, messages: List[Dict[str, Any]],
                                 task_id: str) -> str:
        """Make a final LLM call without tools to synthesize an answer from
        all the context gathered during the ReAct loop."""
        synthesis_prompt = (
            "你已经达到了工具调用次数上限。请基于以上对话中收集到的所有信息，"
            "直接生成完整的最终答案来回答用户的任务。不要再调用任何工具。\n\n"
            "要求：\n"
            "1. 综合所有已获取的信息，给出完整、有条理的回答\n"
            "2. 如果某些信息缺失，基于已有内容尽力回答，标注不确定的部分\n"
            "3. 如果生成了文件，列出文件路径\n"
            "4. 直接输出最终答案，不要解释为什么停止"
        )
        synthesis_messages = messages + [
            {"role": "user", "content": synthesis_prompt},
        ]

        try:
            response = llm_client.chat_with_tools(synthesis_messages, [], temperature=0.3)
            content = response.get("content", "") if response else ""
            if content:
                logger.info(
                    f"Synthesized final answer ({len(content)} chars) "
                    f"for task {task_id}"
                )
                return content
        except Exception as e:
            logger.error(f"Failed to synthesize final answer for {task_id}: {e}")

        return self._build_fallback_summary(messages)

    def _build_fallback_summary(self, messages: List[Dict[str, Any]]) -> str:
        """Build a fallback summary from tool outputs when LLM synthesis fails."""
        tool_outputs = []
        for msg in messages:
            if msg.get("role") == "tool" and msg.get("content"):
                tool_outputs.append(msg["content"])

        if not tool_outputs:
            return "[任务未能完成：达到工具调用次数上限，且未能从执行上下文中提取结果]"

        parts = ["[注意：达到工具调用次数上限，以下为已收集到的部分结果]\n\n"]
        for i, output in enumerate(tool_outputs, 1):
            truncated = output[:500] + "..." if len(output) > 500 else output
            parts.append(f"--- 步骤 {i} 结果 ---\n{truncated}\n")
        return "\n".join(parts)

    def cleanup(self) -> None:
        logger.info("ExecutionAgent cleaned up")
