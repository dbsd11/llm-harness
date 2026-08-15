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


def _execute_bash(command: str, timeout: int = _DEFAULT_CMD_TIMEOUT) -> dict:
    """Run a shell command and return structured result.

    Returns:
        dict with keys: output (str), exit_code (int), timed_out (bool)
    """
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
        return {
            "output": output.strip() or "(no output)",
            "exit_code": result.returncode,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired:
        return {
            "output": f"[error] command timed out after {timeout}s",
            "exit_code": -1,
            "timed_out": True,
        }
    except Exception as e:
        return {
            "output": f"[error] {e}",
            "exit_code": -1,
            "timed_out": False,
        }


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

            loop_result = self._react_loop(
                task_id, question, upstream, server_id=server_id)
            output = loop_result["output"]

            success, error_msg = self._judge_task_success(
                question, output, task_id)

            event_bus.emit("task.execution_completed", {
                "task_id": task_id,
                "role": self.role,
                "response_length": len(output),
                "success": success,
            })

            result = {
                "success": success,
                "output": output,
                "role": self.role,
                "question": question,
            }
            if error_msg:
                result["error"] = error_msg
                logger.warning(f"Task {task_id} execution result: {error_msg}")

            return result

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

    def _judge_task_success(self, goal: str, output: str,
                            task_id: str) -> tuple:
        """Judge whether the task output is correct via LLM evaluation.

        Only the final output correctness matters — tool call failures,
        script errors, and intermediate failures do not determine success.

        Returns:
            (success: bool, error_msg: str or None)
        """
        judge_prompt = (
            "你是一个任务执行结果评判员。请根据以下信息判断任务是否真正成功完成。\n\n"
            f"【任务目标】\n{goal}\n\n"
            f"【Agent 输出】\n{output[:3000]}\n\n"
            "【评判标准】\n"
            "1. 任务是否被实际执行（而非仅提供建议或命令示例）\n"
            "2. 输出内容是否基于实际执行结果（而非推断、猜测或语义分析）\n"
            "3. 是否明确承认无法完成任务（如缺少资源、权限不足、上下文不足等）\n"
            "4. 输出是否包含明显的幻觉内容（如编造的数据、未实际获取的信息）\n\n"
            "【判断规则】\n"
            "- 如果 agent 承认无法获取所需资源但仍生成了基于推断的内容 → 失败\n"
            "- 如果 agent 明确报告任务失败且未生成虚假内容 → 失败\n"
            "- 如果 agent 实际执行了任务并返回了基于真实执行的结果 → 成功\n"
            "- 如果输出包含\"无法完成任务\"、\"上下文不足\"、\"无法获取\"等明确表示失败的内容 → 失败\n\n"
            "请严格按以下 JSON 格式回复，不要包含其他内容：\n"
            '{"success": true/false, "reason": "简短判断理由"}'
        )

        try:
            messages = [
                {"role": "system", "content": "你是任务结果评判员，只输出 JSON。"},
                {"role": "user", "content": judge_prompt},
            ]
            response = llm_client.chat(messages, temperature=0.1)
            if response:
                text = response.strip()
                # Extract JSON from possible markdown wrapping
                if "```" in text:
                    text = text.split("```")[1]
                    if text.startswith("json"):
                        text = text[4:]
                    text = text.strip()
                judgment = json.loads(text)
                success = judgment.get("success", True)
                reason = judgment.get("reason", "")
                logger.info(
                    f"Task {task_id} LLM judgment: success={success}, "
                    f"reason={reason}"
                )
                if not success:
                    return False, f"结果评判为失败: {reason}"
                return True, None
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to parse LLM judgment for {task_id}: {e}")
        except Exception as e:
            logger.error(f"LLM judgment call failed for {task_id}: {e}")

        return True, None

    def _react_loop(self, task_id: str, question: str,
                    upstream: List[str], server_id: str = "") -> dict:
        """ReAct loop: reason -> act (tool) -> observe -> repeat.

        Returns:
            dict with key: output (str)
        """
        # ── Static execution framework (shared across ALL tasks → prefix cache) ──
        system_msg = (
            "你是一个命令执行 Agent，通过 run_bash 工具在服务器 shell 中执行命令来完成任务。\n\n"
            "【执行规则】\n"
            "1. 必须使用 run_bash 工具实际执行命令，禁止只提供命令示例而不执行\n"
            "2. 禁止只解释如何使用命令而不实际调用工具\n"
            "3. 禁止说'我无法执行'或'建议使用以下命令'而不实际调用 run_bash\n"
            "4. 只运行必要的命令，避免重复探索\n"
            "5. 获取到足够信息后，立即生成最终答案\n"
            "6. 最终答案应包含实际执行命令的完整输出结果\n\n"
            "【反幻觉规则 — 最高优先级】\n"
            "1. 所有输出必须严格基于 run_bash 的实际执行结果\n"
            "2. 禁止凭推断、猜测、记忆或语义分析生成任何事实性内容（包括版本号、文件路径、代码片段、配置项等）\n"
            "3. 如果无法获取所需资源（如 git clone 失败、文件不存在），必须立即停止并报告失败原因\n"
            "4. 禁止在无法访问实际数据的情况下生成报告、分析或任何实质性内容\n"
            "5. 宁可报告任务未完成，也不要生成基于猜测的虚假结果\n\n"
            "【文件输出】\n"
            "- 所有生成的文件必须保存到 /data 目录，禁止保存到 /tmp 或 /app\n"
            "- 在最终回答中列出所有生成文件的完整路径\n\n"
            "【工具额度】最多 10 次 run_bash 调用，收集够信息后直接给出完整答案。\n"
        )

        # ── Dynamic per-task content (breaks prefix cache, placed at end) ──
        system_msg += f"\n【当前角色】\n{self.system_prompt}"
        if server_id:
            system_msg += f"\n\n【执行环境】服务器 ID：`{server_id}`"
        if upstream:
            system_msg += "\n\n【前序任务输出】\n" + "\n---\n".join(upstream)

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
                    bash_result = _execute_bash(cmd, timeout)
                    tool_output = bash_result["output"]
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

        return {"output": final_text}

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
