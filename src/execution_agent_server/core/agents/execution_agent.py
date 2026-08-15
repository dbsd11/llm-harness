# Execution Agent - ReAct agent with bash tool
import json
import os
import subprocess
from typing import Dict, Any, List, Optional
from .base_agent import BaseAgent
from core.llm_client import llm_client
from core.event_bus import event_bus
from logger import logger

_MAX_REACT_ITERATIONS = 20
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


def _write_upstream_files(upstream: List[str], task_id: str) -> List[str]:
    """Write upstream outputs to /data/upstream/ for on-demand grep retrieval.

    Returns list of file paths written.
    """
    if not upstream:
        return []
    upstream_dir = "/data/upstream"
    os.makedirs(upstream_dir, exist_ok=True)
    paths = []
    for i, content in enumerate(upstream):
        path = os.path.join(upstream_dir, f"task_{i}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        paths.append(path)
        logger.info(
            f"Wrote upstream output {i} ({len(content)} chars) to {path} "
            f"for task {task_id}"
        )
    return paths


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

            upstream_files = _write_upstream_files(upstream, task_id)

            loop_result = self._react_loop(
                task_id, question, upstream_files, server_id=server_id)
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
        """Score task output relevance to goal on a 0-100 scale.

        Score >= 80 → success. Below 80 → failure.

        Returns:
            (success: bool, error_msg: str or None)
        """
        judge_prompt = (
            "你是一个任务执行结果评判员。请对 Agent 的输出与任务目标的相关性和完成度打分（0-100）。\n\n"
            f"【任务目标】\n{goal}\n\n"
            f"【Agent 输出】\n{output[:8000]}\n\n"
            "【评分维度】\n"
            "1. 目标覆盖度（40分）：输出是否涵盖了任务目标要求的主要内容\n"
            "2. 实际执行证据（30分）：输出是否包含实际执行命令的结果（如 git clone 输出、文件内容、命令输出等）\n"
            "3. 信息相关性（20分）：输出内容是否与任务目标直接相关，而非无关信息\n"
            "4. 结构完整性（10分）：输出是否有合理的结构和组织\n\n"
            "【评分规则】\n"
            "- 90-100：完整覆盖目标，有明确执行证据，内容高度相关\n"
            "- 80-89：基本覆盖目标，有执行证据，个别细节可能不精确但不影响整体\n"
            "- 60-79：部分覆盖目标，有一定执行证据但不够完整\n"
            "- 0-59：严重偏离目标，或明显未实际执行，或大量幻觉内容\n\n"
            "【注意】\n"
            "- 不要因为个别版本号、日期等细节无法验证就大幅扣分\n"
            "- 重点关注输出是否实际回应了任务目标的核心需求\n"
            "- 如果 Agent 实际执行了命令（如 git clone）并基于结果生成了报告，即使部分细节有偏差，也应给较高分\n"
            "- 如果任务要求生成文件并保存到 /data，Agent 将文件保存并用 cat 输出了内容，应视为有效执行\n"
            "- 如果 Agent 输出了结构化的报告内容（无论是否内联全文或保存为文件），且内容与任务目标高度相关，不应因'未内联全文'而大幅扣分\n\n"
            "请严格按以下 JSON 格式回复，不要包含其他内容：\n"
            '{"score": 0-100, "reason": "简短评分理由"}'
        )

        try:
            messages = [
                {"role": "system", "content": "你是任务结果评判员，只输出 JSON。"},
                {"role": "user", "content": judge_prompt},
            ]
            response = llm_client.chat(messages, temperature=0.1)
            if response:
                text = response.strip()
                if "```" in text:
                    text = text.split("```")[1]
                    if text.startswith("json"):
                        text = text[4:]
                    text = text.strip()
                judgment = json.loads(text)
                score = judgment.get("score", 0)
                reason = judgment.get("reason", "")
                success = score >= 80
                logger.info(
                    f"Task {task_id} LLM judgment: score={score}, "
                    f"success={success}, reason={reason}"
                )
                if not success:
                    return False, f"结果评判得分 {score}/100 (低于80分): {reason}"
                return True, None
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to parse LLM judgment for {task_id}: {e}")
        except Exception as e:
            logger.error(f"LLM judgment call failed for {task_id}: {e}")

        return True, None

    def _react_loop(self, task_id: str, question: str,
                    upstream_files: List[str], server_id: str = "") -> dict:
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
            "2. 禁止凭推断、猜测、记忆或语义分析生成任何事实性内容\n"
            "3. 如果无法获取所需资源，必须立即停止并报告失败原因\n"
            "4. 禁止在无法访问实际数据的情况下生成报告或分析\n"
            "5. 宁可报告任务未完成，也不要生成基于猜测的虚假结果\n\n"
            "【文件输出】\n"
            "- 生成的文件保存到 /data 目录\n"
            "- 保存后用 cat 读取文件内容，将完整内容包含在最终回答中\n\n"
            "【信息检索】\n"
            "- 使用 grep -n '关键词' /data/upstream/*.md 检索前序任务输出中的关键信息\n"
            "- 使用 cat /data/upstream/task_N.md 读取完整的前序任务输出\n"
            "- 按需检索，不要一次性读取所有文件\n\n"
            "【工具额度】最多 20 次 run_bash 调用，收集够信息后直接给出完整答案。\n"
        )

        # ── Dynamic per-task content (breaks prefix cache, placed at end) ──
        system_msg += f"\n【当前角色】\n{self.system_prompt}"
        if server_id:
            system_msg += f"\n\n【执行环境】服务器 ID：`{server_id}`"
        if upstream_files:
            system_msg += "\n\n【前序任务输出文件】\n"
            system_msg += "以下文件包含前序任务的完整输出，使用 grep/cat 按需检索：\n"
            for path in upstream_files:
                system_msg += f"- {path}\n"

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
                if len(content) < 200 and iteration > 0:
                    logger.warning(
                        f"ReAct loop: model produced short response "
                        f"({len(content)} chars) at iteration {iteration + 1} "
                        f"for task {task_id}, synthesizing from context"
                    )
                    final_text = self._synthesize_final_answer(messages, task_id)
                else:
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
            "直接生成完整的最终答案。\n\n"
            "【严格要求】\n"
            "1. 直接输出完整的回答内容，禁止使用\"基于已收集的信息\"、\"我将生成\"、"
            "\"现在给出\"等元描述语句开头\n"
            "2. 回复长度不得少于 500 字，必须包含实质性的分析内容\n"
            "3. 综合所有已获取的工具执行结果，给出完整、有条理的回答\n"
            "4. 如果生成了文件，列出文件路径\n"
            "5. 不要解释为什么停止，不要描述你要做什么——直接做"
        )
        synthesis_messages = messages + [
            {"role": "user", "content": synthesis_prompt},
        ]

        try:
            response = llm_client.chat_with_tools(synthesis_messages, [], temperature=0.3)
            content = response.get("content", "") if response else ""
            if content and len(content) >= 200:
                logger.info(
                    f"Synthesized final answer ({len(content)} chars) "
                    f"for task {task_id}"
                )
                return content
            logger.warning(
                f"Synthesized answer too short ({len(content) if content else 0} chars) "
                f"for task {task_id}, using fallback"
            )
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
            truncated = output[:2000] + "..." if len(output) > 2000 else output
            parts.append(f"--- 步骤 {i} 结果 ---\n{truncated}\n")
        return "\n".join(parts)

    def cleanup(self) -> None:
        logger.info("ExecutionAgent cleaned up")
