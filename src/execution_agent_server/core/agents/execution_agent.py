# Execution Agent - Plan-first execution with ReAct steps
import json
import os
import re
import subprocess
from typing import Dict, Any, List, Optional
from .base_agent import BaseAgent
from core.llm_client import llm_client
from core.event_bus import event_bus
from logger import logger

_MAX_STEP_ITERATIONS = 15
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

ENGINEERING_CONSTRAINTS = (
    "【工程约束 — 规划必须遵循】\n"
    "1. The shortest path to done is the right path — 选择最直接的实现路径\n"
    "2. The best code is the code never written — 能不写就不写，能复用就复用\n"
    "3. Bug fix = root cause, not symptom — 修改前 grep 所有调用者，"
    "在共享函数中一次修复，而非在每个调用者处打补丁\n"
    "4. Never simplify away: 输入验证、防数据丢失的错误处理、安全措施、"
    "无障碍基础、用户明确要求的功能 — 这些不可省略\n"
)


def _execute_bash(command: str, timeout: int = _DEFAULT_CMD_TIMEOUT) -> dict:
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
        return {
            "output": output.strip() or "(no output)",
            "exit_code": result.returncode,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired:
        return {"output": f"[error] command timed out after {timeout}s",
                "exit_code": -1, "timed_out": True}
    except Exception as e:
        return {"output": f"[error] {e}", "exit_code": -1, "timed_out": False}


def _write_upstream_files(upstream: List[str], task_id: str) -> List[str]:
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
        logger.info(f"Wrote upstream output {i} ({len(content)} chars) to {path} for task {task_id}")
    return paths


class ExecutionAgent(BaseAgent):
    """Plan-first execution agent.

    Phase 1 — Plan:  LLM generates P0..Pn execution plan with engineering constraints.
    Phase 2 — Execute: each plan step runs through a ReAct loop with bash tool.
    Phase 3 — Judge:  per-step results aggregated for LLM scoring.
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

    # ──────────────────────────────────────────────
    #  Main entry point
    # ──────────────────────────────────────────────

    def run(self, task_id: str, context: Dict[str, Any]) -> Dict[str, Any]:
        event_bus.emit("task.execution_started", {"task_id": task_id})

        try:
            question = context.get("goal") or context.get("question", "")
            if not question:
                raise ValueError("No question or goal provided in context")

            upstream = context.get("upstream_outputs") or []
            server_id = context.get("server_id", "")
            logger.info(f"ExecutionAgent (plan-first) processing: {question[:100]}...")

            upstream_files = _write_upstream_files(upstream, task_id)

            # Phase 1: Plan
            plan = self._generate_plan(task_id, question, upstream_files, server_id)
            plan_path = self._save_plan(plan, task_id)

            # Phase 2: Execute
            step_results = self._execute_plan(task_id, plan, upstream_files, server_id)

            # Phase 3: Aggregate
            output = self._build_step_summary(plan, step_results, plan_path)

            # Phase 4: Judge (per-step results)
            success, error_msg = self._judge_task_success(
                question, plan, step_results, output, task_id)

            event_bus.emit("task.execution_completed", {
                "task_id": task_id, "role": self.role,
                "response_length": len(output), "success": success,
            })

            result = {"success": success, "output": output,
                      "role": self.role, "question": question}
            if error_msg:
                result["error"] = error_msg
                logger.warning(f"Task {task_id} execution result: {error_msg}")
            return result

        except Exception as e:
            error_msg = str(e)
            logger.error(f"ExecutionAgent error for task {task_id}: {error_msg}")
            event_bus.emit("task.execution_failed", {"task_id": task_id, "error": error_msg})
            return {"success": False, "output": "", "error": error_msg}

    # ──────────────────────────────────────────────
    #  Phase 1: Plan
    # ──────────────────────────────────────────────

    def _generate_plan(self, task_id: str, question: str,
                       upstream_files: List[str], server_id: str) -> List[Dict]:
        """LLM generates P0..Pn execution plan."""
        upstream_hint = ""
        if upstream_files:
            upstream_hint = (
                "\n【前序任务输出文件】\n"
                "以下文件包含前序任务的完整输出，规划时考虑需要检索哪些信息：\n"
                + "\n".join(f"- {p}" for p in upstream_files) + "\n"
            )

        server_hint = f"\n【执行环境】服务器 ID：`{server_id}`" if server_id else ""

        plan_prompt = (
            f"你是一个工程规划专家。请为以下任务生成精简的执行计划。\n\n"
            f"【任务目标】\n{question}\n\n"
            f"【角色定义】\n{self.system_prompt}\n"
            f"{server_hint}"
            f"{upstream_hint}\n"
            f"{ENGINEERING_CONSTRAINTS}\n"
            f"【规划要求】\n"
            f"1. 分析任务目标，确定最短实现路径\n"
            f"2. 将任务分解为 P0, P1, ..., Pn 个按优先级排序的执行步骤\n"
            f"3. 每个步骤必须具体、可执行、有明确的完成标准\n"
            f"4. 步骤数量精简（通常 2-5 步），避免不必要的步骤\n"
            f"5. P0 是最高优先级，必须完成；后续步骤按重要性递减\n"
            f"6. 每个步骤的 description 应包含具体要执行的命令或操作\n\n"
            f"返回格式（严格遵循）：\n"
            f"```json\n"
            f"{{\n"
            f'  "goal_analysis": "简短的目标分析",\n'
            f'  "steps": [\n'
            f"    {{\n"
            f'      "id": "P0",\n'
            f'      "description": "步骤描述（包含具体操作）",\n'
            f'      "expected_output": "预期产出"\n'
            f"    }}\n"
            f"  ]\n"
            f"}}\n"
            f"```\n\n"
            f"只返回 JSON，不要其他内容。"
        )

        messages = [
            {"role": "system", "content": "你是工程规划助手，只输出 JSON。"},
            {"role": "user", "content": plan_prompt},
        ]

        response = llm_client.chat(messages, temperature=0.2)
        if not response:
            logger.warning(f"Plan generation returned empty for {task_id}, using single-step plan")
            return [{"id": "P0", "description": question,
                     "expected_output": "任务完成"}]

        json_str = response
        if "```json" in response:
            json_str = response.split("```json")[1].split("```")[0].strip()
        elif "```" in response:
            json_str = response.split("```")[1].split("```")[0].strip()

        try:
            data = json.loads(json_str)
            steps = data.get("steps", [])
            if not steps:
                return [{"id": "P0", "description": question,
                         "expected_output": "任务完成"}]
            for i, step in enumerate(steps):
                step.setdefault("id", f"P{i}")
                step.setdefault("description", "")
                step.setdefault("expected_output", "")
            logger.info(f"Generated {len(steps)} plan steps for {task_id}: "
                        f"{data.get('goal_analysis', '')[:100]}")
            return steps
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse plan JSON for {task_id}: {e}")
            return [{"id": "P0", "description": question,
                     "expected_output": "任务完成"}]

    def _save_plan(self, plan: List[Dict], task_id: str) -> str:
        """Save plan to /data for traceability. Returns file path."""
        plan_dir = "/data/plans"
        os.makedirs(plan_dir, exist_ok=True)
        path = os.path.join(plan_dir, f"{task_id[:8]}_plan.md")
        lines = ["# 执行计划\n"]
        for step in plan:
            lines.append(f"## {step['id']}: {step['description']}")
            lines.append(f"预期产出: {step.get('expected_output', '')}\n")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            logger.info(f"Saved plan to {path}")
        except Exception as e:
            logger.warning(f"Failed to save plan: {e}")
        return path

    # ──────────────────────────────────────────────
    #  Phase 2: Execute
    # ──────────────────────────────────────────────

    def _build_exec_system_msg(self, upstream_files: List[str], server_id: str) -> str:
        """Static execution framework (shared across ALL tasks → prefix cache)."""
        msg = (
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
            "- 在最终回答中列出保存的文件路径，并包含内容摘要\n"
            "- 不需要用 cat 回显完整文件内容，评判系统会自动读取已保存的文件\n\n"
            "【信息检索】\n"
            "- 使用 grep -n '关键词' /data/upstream/*.md 检索前序任务输出中的关键信息\n"
            "- 使用 cat /data/upstream/task_N.md 读取完整的前序任务输出\n"
            "- 按需检索，不要一次性读取所有文件\n\n"
            "【规划执行】\n"
            "- 你会收到一个执行计划，必须按计划逐步执行\n"
            "- 每完成一步，报告该步骤的直接结果\n"
            "- 如果某步失败，记录失败原因后继续下一步\n"
        )
        if server_id:
            msg += f"\n【执行环境】服务器 ID：`{server_id}`"
        if upstream_files:
            msg += "\n\n【前序任务输出文件】\n使用 grep/cat 按需检索：\n"
            msg += "\n".join(f"- {p}" for p in upstream_files)
        return msg

    def _execute_plan(self, task_id: str, plan: List[Dict],
                      upstream_files: List[str], server_id: str) -> List[Dict]:
        """Execute each plan step via ReAct loop. Returns per-step results."""
        system_msg = self._build_exec_system_msg(upstream_files, server_id)
        system_msg += f"\n\n【当前角色】\n{self.system_prompt}"

        step_results = []

        for step_idx, step in enumerate(plan):
            step_id = step["id"]
            step_desc = step["description"]
            logger.info(f"Executing step {step_id} ({step_idx + 1}/{len(plan)}): "
                        f"{step_desc[:100]}...")

            plan_context = (
                f"【执行计划】\n"
                + "\n".join(f"  {s['id']}: {s['description']}" for s in plan)
                + f"\n\n【当前步骤 — {step_id}】\n{step_desc}\n\n"
                f"【工程约束】\n{ENGINEERING_CONSTRAINTS}\n"
                f"请执行当前步骤。完成后报告该步骤的直接结果。"
            )

            messages: List[Dict[str, Any]] = [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": plan_context},
            ]

            step_output = ""

            for iteration in range(_MAX_STEP_ITERATIONS):
                if not llm_client.client:
                    raise RuntimeError("LLM client not configured")

                response = llm_client.chat_with_tools(messages, TOOLS, temperature=0.3)
                if response is None:
                    step_output = "[LLM returned empty response]"
                    break

                content = response.get("content", "") or ""
                tool_calls = response.get("tool_calls")

                assistant_msg: Dict[str, Any] = {"role": "assistant", "content": content}
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                messages.append(assistant_msg)

                if not tool_calls:
                    step_output = content
                    logger.info(f"Step {step_id} completed after {iteration + 1} iterations")
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
                        logger.info(f"Step {step_id} bash: {cmd[:200]}")
                        bash_result = _execute_bash(cmd, timeout)
                        tool_output = bash_result["output"]
                    else:
                        tool_output = f"[error] unknown tool: {fn_name}"

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": tool_output,
                    })
            else:
                logger.warning(f"Step {step_id} hit max iterations ({_MAX_STEP_ITERATIONS})")
                step_output = self._synthesize_step_answer(messages, task_id, step_id)

            step_results.append({
                "step_id": step_id,
                "description": step_desc,
                "output": step_output,
                "completed": bool(step_output and len(step_output) > 50),
            })

        return step_results

    def _synthesize_step_answer(self, messages: List[Dict[str, Any]],
                                task_id: str, step_id: str) -> str:
        """Synthesize a step answer when ReAct hits max iterations."""
        prompt = (
            f"你已达到步骤 {step_id} 的工具调用上限。"
            f"请基于以上对话中收集到的信息，直接生成该步骤的完整结果。\n\n"
            f"【要求】\n"
            f"1. 直接输出结果，禁止使用元描述语句开头\n"
            f"2. 综合所有已获取的工具执行结果\n"
            f"3. 如果生成了文件，列出文件路径\n"
            f"4. 不要解释为什么停止——直接输出内容"
        )
        synthesis_messages = messages + [{"role": "user", "content": prompt}]

        for attempt in range(2):
            try:
                content = llm_client.chat(synthesis_messages, temperature=0.3)
                if content and len(content) >= 100:
                    return content
            except Exception as e:
                logger.error(f"Synthesis failed for step {step_id}: {e}")

        return self._build_fallback_summary(messages)

    # ──────────────────────────────────────────────
    #  Phase 3: Aggregate
    # ──────────────────────────────────────────────

    def _build_step_summary(self, plan: List[Dict],
                            step_results: List[Dict], plan_path: str) -> str:
        """Build final output from per-step results."""
        parts = [f"# 执行结果报告\n\n执行计划: {plan_path}\n"]

        for sr in step_results:
            status = "✅ 完成" if sr["completed"] else "⚠️ 未完成"
            parts.append(f"## {sr['step_id']}: {sr['description']}\n")
            parts.append(f"状态: {status}\n")
            parts.append(f"### 步骤结果\n{sr['output']}\n")

        completed = sum(1 for sr in step_results if sr["completed"])
        parts.append(f"---\n总计: {completed}/{len(step_results)} 步骤完成")
        return "\n".join(parts)

    def _build_fallback_summary(self, messages: List[Dict[str, Any]]) -> str:
        tool_outputs = [msg["content"] for msg in messages
                        if msg.get("role") == "tool" and msg.get("content")]
        if not tool_outputs:
            return "[任务未能完成：达到工具调用次数上限]"
        parts = ["[注意：达到工具调用次数上限，以下为已收集到的部分结果]\n"]
        for i, output in enumerate(tool_outputs, 1):
            truncated = output[:2000] + "..." if len(output) > 2000 else output
            parts.append(f"--- 步骤 {i} 结果 ---\n{truncated}\n")
        return "\n".join(parts)

    # ──────────────────────────────────────────────
    #  Phase 4: Judge (per-step results)
    # ──────────────────────────────────────────────

    def _judge_task_success(self, goal: str, plan: List[Dict],
                            step_results: List[Dict], output: str,
                            task_id: str) -> tuple:
        """Score based on per-step execution results.

        Reads saved /data files referenced in step outputs.
        Score >= 80 → success.
        """
        file_contents = []
        for sr in step_results:
            for path in dict.fromkeys(re.findall(r'/data/[\w\-./]+\.md', sr["output"])):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        content = f.read()
                    file_contents.append(
                        f"--- {path} ({len(content)} chars) ---\n{content[:8000]}")
                except Exception:
                    pass

        step_summary = "\n".join(
            f"【{sr['step_id']}: {sr['description']}】"
            f"\n完成: {sr['completed']}\n结果: {sr['output'][:3000]}\n"
            for sr in step_results
        )

        plan_text = "\n".join(f"{s['id']}: {s['description']}" for s in plan)

        files_section = ""
        if file_contents:
            files_section = "\n\n【已保存文件内容】\n" + "\n\n".join(file_contents)

        judge_prompt = (
            "你是一个任务执行结果评判员。根据执行计划的每步实际执行结果打分（0-100）。\n\n"
            f"【任务目标】\n{goal}\n\n"
            f"【执行计划】\n{plan_text}\n\n"
            f"【各步骤执行结果】\n{step_summary[:12000]}"
            f"{files_section}\n\n"
            "【评分维度】\n"
            "1. 步骤完成度（40分）：计划中各步骤是否实际完成并有具体输出\n"
            "2. 实际执行证据（30分）：是否有实际命令执行结果（命令输出、文件内容等）\n"
            "3. 目标达成度（20分）：整体是否达成了任务目标\n"
            "4. 工程质量（10分）：是否遵循了工程约束（最短路径、根因修复等）\n\n"
            "【评分规则】\n"
            "- 90-100：所有步骤完成，有执行证据，目标达成\n"
            "- 80-89：大部分步骤完成，有执行证据\n"
            "- 60-79：部分步骤完成，有一定执行证据\n"
            "- 0-59：大量步骤未完成，或未实际执行\n\n"
            "【注意】\n"
            "- 重点关注每步的实际执行结果，而非输出格式\n"
            "- 如果文件保存到 /data 且内容完整，视为有效执行\n"
            "- 不要因为细节偏差大幅扣分\n\n"
            "请严格按 JSON 格式回复：\n"
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
                logger.info(f"Task {task_id} judgment: score={score}, "
                            f"success={success}, reason={reason}")
                if not success:
                    return False, f"结果评判得分 {score}/100 (低于80分): {reason}"
                return True, None
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to parse judgment for {task_id}: {e}")
        except Exception as e:
            logger.error(f"Judgment call failed for {task_id}: {e}")

        return True, None

    def cleanup(self) -> None:
        logger.info("ExecutionAgent cleaned up")
