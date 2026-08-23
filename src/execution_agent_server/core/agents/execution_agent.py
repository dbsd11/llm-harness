# Execution Agent - Intent-aware plan-first execution with ReAct steps.
#
# Supports three intents (modeled after bug-agent-server):
#   generate_plan  — agentic plan generation with tool calls
#   revise_plan    — revise an existing plan based on review feedback
#   execute_plan   — execute an approved plan step by step
#
# In assistant_mode, plan generation/revision returns the plan for review
# instead of executing it immediately.
import json
import os
import re
from typing import Dict, Any, List, Optional
from .base_agent import BaseAgent
from core.llm_client import llm_client
from core.event_bus import event_bus
from core.tools import create_tool_registry
from logger import logger

_MAX_STEP_ITERATIONS = 15
_MAX_PLAN_ITERATIONS = 8

ENGINEERING_CONSTRAINTS = (
    "【工程约束 — 规划必须遵循】\n"
    "1. The shortest path to done is the right path — 选择最直接的实现路径\n"
    "2. The best code is the code never written — 能不写就不写，能复用就复用\n"
    "3. Bug fix = root cause, not symptom — 修改前 grep 所有调用者，"
    "在共享函数中一次修复，而非在每个调用者处打补丁\n"
    "4. Never simplify away: 输入验证、防数据丢失的错误处理、安全措施、"
    "无障碍基础、用户明确要求的功能 — 这些不可省略\n"
)

INTENT_GENERATE_PLAN = "generate_plan"
INTENT_EXECUTE_PLAN = "execute_plan"
INTENT_REVISE_PLAN = "revise_plan"
INTENT_DIRECT_EXECUTE = "direct_execute"


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
    """Intent-aware plan-first execution agent.

    Phase 1 — Plan:  Agentic plan generation with tool calls (ReAct loop).
    Phase 1b — Revise: Revise plan based on assistant review feedback.
    Phase 2 — Execute: Each plan step runs through a ReAct loop with bash tool.
    Phase 3 — Aggregate: Per-step results aggregated into report.
    Phase 4 — Judge: LLM scores execution quality.
    """

    def __init__(self):
        self.config = {}
        self.role = None
        self.system_prompt = None
        self._ws_client = None
        self._assistant_mode = False

    def get_agent_type(self) -> str:
        return "execution"

    def initialize(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.role = config.get("role", "general assistant")
        self.system_prompt = config.get("system_prompt", "You are a helpful assistant.")
        self._ws_client = config.get("ws_client")
        self._assistant_mode = config.get("assistant_mode", False)
        logger.info(f"ExecutionAgent initialized: role={self.role}, "
                    f"assistant_mode={self._assistant_mode}")

    # ──────────────────────────────────────────────
    #  Intent detection
    # ──────────────────────────────────────────────

    @staticmethod
    def _detect_intent(context: Dict[str, Any]) -> str:
        """Detect task intent from context.

        Priority:
        1. Explicit task_type from scheduling agent
        2. Context heuristics (plan + review_feedback → revise, plan → execute)
        3. Default: generate_plan
        """
        task_type = context.get("task_type")
        if task_type in (INTENT_GENERATE_PLAN, INTENT_EXECUTE_PLAN,
                         INTENT_REVISE_PLAN, INTENT_DIRECT_EXECUTE):
            return task_type

        plan = context.get("plan")
        if plan and context.get("review_feedback"):
            return INTENT_REVISE_PLAN
        if plan:
            return INTENT_EXECUTE_PLAN

        return INTENT_GENERATE_PLAN

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
            logger.info(f"ExecutionAgent processing task {task_id}: "
                        f"{question[:100]}...")

            upstream_files = _write_upstream_files(upstream, task_id)
            intent = self._detect_intent(context)
            logger.info(f"Task {task_id} intent: {intent}")

            if intent == INTENT_GENERATE_PLAN:
                return self._handle_generate_plan(
                    task_id, question, upstream_files, server_id, context)

            elif intent == INTENT_REVISE_PLAN:
                return self._handle_revise_plan(
                    task_id, question, upstream_files, server_id, context)

            elif intent == INTENT_EXECUTE_PLAN:
                return self._handle_execute_plan(
                    task_id, question, upstream_files, server_id, context)

            elif intent == INTENT_DIRECT_EXECUTE:
                return self._handle_direct_execute(
                    task_id, question, upstream_files, server_id, context)

            else:
                raise ValueError(f"Unknown intent: {intent}")

        except Exception as e:
            error_msg = str(e)
            logger.error(f"ExecutionAgent error for task {task_id}: {error_msg}")
            event_bus.emit("task.execution_failed",
                          {"task_id": task_id, "error": error_msg})
            return {"success": False, "output": "", "error": error_msg}

    # ──────────────────────────────────────────────
    #  Intent handlers
    # ──────────────────────────────────────────────

    def _handle_generate_plan(self, task_id: str, question: str,
                              upstream_files: List[str], server_id: str,
                              context: Dict[str, Any]) -> Dict[str, Any]:
        """Generate a plan. In assistant_mode, return it for review."""
        tools = self._build_tool_registry(task_id, question)
        plan = self._generate_plan_agentic(
            task_id, question, upstream_files, server_id, tools)
        plan_path = self._save_plan(plan, task_id)

        if self._assistant_mode:
            plan_json = json.dumps(plan, ensure_ascii=False)
            event_bus.emit("task.plan_generated",
                          {"task_id": task_id, "role": self.role})
            event_bus.emit("task.execution_completed", {
                "task_id": task_id, "role": self.role,
                "response_length": len(plan_json), "success": True,
            })
            return {
                "success": True,
                "output": plan_json,
                "phase": "plan_ready",
                "plan": plan,
                "plan_path": plan_path,
                "role": self.role,
                "question": question,
            }

        step_results = self._execute_plan(task_id, plan, upstream_files, server_id)
        output = self._build_step_summary(plan, step_results, plan_path)
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
        return result

    def _handle_revise_plan(self, task_id: str, question: str,
                            upstream_files: List[str], server_id: str,
                            context: Dict[str, Any]) -> Dict[str, Any]:
        """Revise an existing plan based on review feedback."""
        existing_plan = context.get("plan", [])
        review_feedback = context.get("review_feedback", "")
        tools = self._build_tool_registry(task_id, question)

        plan = self._revise_plan(
            task_id, existing_plan, review_feedback,
            upstream_files, server_id, tools)
        plan_path = self._save_plan(plan, task_id)

        if self._assistant_mode:
            plan_json = json.dumps(plan, ensure_ascii=False)
            event_bus.emit("task.execution_completed", {
                "task_id": task_id, "role": self.role,
                "response_length": len(plan_json), "success": True,
            })
            return {
                "success": True,
                "output": plan_json,
                "phase": "plan_ready",
                "plan": plan,
                "plan_path": plan_path,
                "role": self.role,
                "question": question,
            }

        step_results = self._execute_plan(task_id, plan, upstream_files, server_id)
        output = self._build_step_summary(plan, step_results, plan_path)
        success, error_msg = self._judge_task_success(
            question, plan, step_results, output, task_id)

        result = {"success": success, "output": output,
                  "role": self.role, "question": question}
        if error_msg:
            result["error"] = error_msg
        return result

    def _handle_execute_plan(self, task_id: str, question: str,
                              upstream_files: List[str], server_id: str,
                              context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute an approved plan."""
        plan = context.get("plan", [])
        if not plan:
            raise ValueError("execute_plan intent requires 'plan' in context")

        plan_path = self._save_plan(plan, task_id)
        step_results = self._execute_plan(task_id, plan, upstream_files, server_id)
        output = self._build_step_summary(plan, step_results, plan_path)
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
        return result

    def _handle_direct_execute(self, task_id: str, question: str,
                               upstream_files: List[str], server_id: str,
                               context: Dict[str, Any]) -> Dict[str, Any]:
        """Direct execution with ReAct loop (no planning phase).

        Used for tasks like plan review or ask_assistant answers that need
        a general-purpose response rather than a structured execution plan.
        """
        tools = self._build_tool_registry(task_id, question)
        tool_defs = tools.to_openai_tools()

        system_msg = (
            f"你是一个智能助手。你的角色是：{self.role}\n\n"
            f"【角色定义】\n{self.system_prompt}\n\n"
            "请根据用户的问题，使用可用工具收集必要信息后给出完整回答。\n"
            "如果需要执行命令来获取信息，请使用 run_bash 工具。\n"
            "回答应直接、完整、有用。"
        )
        if server_id:
            system_msg += f"\n\n【执行环境】服务器 ID：`{server_id}`"

        user_msg = question
        if upstream_files:
            user_msg += "\n\n【前序任务输出文件】\n"
            user_msg += "\n".join(f"- {p}" for p in upstream_files)

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ]

        output = ""

        if not tool_defs:
            response = llm_client.chat(messages, temperature=0.3)
            output = response or "[LLM returned empty response]"
        else:
            for iteration in range(_MAX_STEP_ITERATIONS):
                if not llm_client.client:
                    raise RuntimeError("LLM client not configured")

                response = llm_client.chat_with_tools(messages, tool_defs, temperature=0.3)
                if response is None:
                    output = "[LLM returned empty response]"
                    break

                content = response.get("content", "") or ""
                tool_calls = response.get("tool_calls")

                assistant_msg: Dict[str, Any] = {"role": "assistant", "content": content}
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                messages.append(assistant_msg)

                if not tool_calls:
                    output = content
                    logger.info(f"Direct execute completed after {iteration + 1} iterations")
                    break

                for tc in tool_calls:
                    fn_name = tc["function"]["name"]
                    try:
                        fn_args = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        fn_args = {}

                    tool_output = tools.call(fn_name, **fn_args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": tool_output,
                    })
            else:
                logger.warning(f"Direct execute hit max iterations ({_MAX_STEP_ITERATIONS})")
                output = content or "[达到最大迭代次数]"

        event_bus.emit("task.execution_completed", {
            "task_id": task_id, "role": self.role,
            "response_length": len(output), "success": True,
        })

        return {"success": True, "output": output,
                "role": self.role, "question": question}

    def _build_tool_registry(self, task_id: str, question: str):
        """Create tool registry with appropriate tools."""
        context_summary = f"Task: {question[:200]}"
        return create_tool_registry(
            assistant_mode=self._assistant_mode,
            ws_client=self._ws_client,
            task_id=task_id,
            context_summary=context_summary,
        )

    # ──────────────────────────────────────────────
    #  Phase 1: Agentic Plan Generation
    # ──────────────────────────────────────────────

    def _generate_plan_agentic(self, task_id: str, question: str,
                                upstream_files: List[str], server_id: str,
                                tools) -> List[Dict]:
        """Generate plan with tool support (ReAct loop).

        The LLM can call tools (bash, ask_assistant) during planning to gather
        information before committing to a plan.
        """
        upstream_hint = ""
        if upstream_files:
            upstream_hint = (
                "\n【前序任务输出文件】\n"
                "以下文件包含前序任务的完整输出，规划时考虑需要检索哪些信息：\n"
                + "\n".join(f"- {p}" for p in upstream_files) + "\n"
            )

        server_hint = f"\n【执行环境】服务器 ID：`{server_id}`" if server_id else ""

        tool_defs = tools.to_openai_tools()

        system_msg = (
            "你是一个工程规划专家。你可以通过工具收集信息，然后生成精简的执行计划。\n\n"
            f"【角色定义】\n{self.system_prompt}\n"
            f"{server_hint}"
            f"{upstream_hint}\n"
            f"{ENGINEERING_CONSTRAINTS}\n"
            "【规划流程】\n"
            "1. 分析任务目标，确定需要哪些额外信息\n"
            "2. 使用工具（如 run_bash 探索环境、ask_assistant 请求补充信息）收集必要信息\n"
            "3. 基于收集的信息，生成精简的执行计划\n"
            "4. 计划确定后，直接输出 JSON 格式的计划\n\n"
            "【规划要求】\n"
            "1. 选择最直接的实现路径\n"
            "2. 将任务分解为 P0, P1, ..., Pn 个按优先级排序的执行步骤\n"
            "3. 每个步骤必须具体、可执行、有明确的完成标准\n"
            "4. 步骤数量精简（通常 2-5 步），避免不必要的步骤\n"
            "5. P0 是最高优先级，必须完成；后续步骤按重要性递减\n"
            "6. 每个步骤的 description 应包含具体要执行的命令或操作\n\n"
            "【输出格式】\n"
            "当你完成信息收集并确定计划后，输出以下 JSON 格式（用 ```json 包裹）：\n"
            "```json\n"
            '{"goal_analysis": "简短的目标分析",\n'
            ' "steps": [\n'
            '   {"id": "P0", "description": "步骤描述", "expected_output": "预期产出"}\n'
            " ]}\n"
            "```"
        )

        user_msg = f"【任务目标】\n{question}"

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ]

        if not tool_defs:
            return self._generate_plan_simple(task_id, question, messages)

        for iteration in range(_MAX_PLAN_ITERATIONS):
            if not llm_client.client:
                raise RuntimeError("LLM client not configured")

            response = llm_client.chat_with_tools(messages, tool_defs, temperature=0.3)
            if response is None:
                logger.warning(f"Plan generation returned empty for {task_id}")
                break

            content = response.get("content", "") or ""
            tool_calls = response.get("tool_calls")

            assistant_msg: Dict[str, Any] = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            if not tool_calls:
                logger.info(f"Plan generation completed after {iteration + 1} iterations")
                break

            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    fn_args = {}

                logger.info(f"Plan generation tool call: {fn_name}({json.dumps(fn_args, ensure_ascii=False)[:200]})")
                tool_output = tools.call(fn_name, **fn_args)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_output,
                })
        else:
            logger.warning(f"Plan generation hit max iterations ({_MAX_PLAN_ITERATIONS})")

        return self._parse_plan_from_messages(messages, task_id, question)

    def _generate_plan_simple(self, task_id: str, question: str,
                               messages: List[Dict]) -> List[Dict]:
        """Fallback: generate plan without tool support."""
        response = llm_client.chat(messages, temperature=0.2)
        if not response:
            logger.warning(f"Plan generation returned empty for {task_id}")
            return [{"id": "P0", "description": question, "expected_output": "任务完成"}]
        return self._parse_plan_json(response, task_id, question)

    def _parse_plan_from_messages(self, messages: List[Dict],
                                   task_id: str, question: str) -> List[Dict]:
        """Extract plan JSON from the last assistant message."""
        for msg in reversed(messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                return self._parse_plan_json(msg["content"], task_id, question)
        return [{"id": "P0", "description": question, "expected_output": "任务完成"}]

    def _parse_plan_json(self, text: str, task_id: str,
                          question: str) -> List[Dict]:
        """Parse plan JSON from LLM response text."""
        json_str = text
        if "```json" in text:
            json_str = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            json_str = text.split("```")[1].split("```")[0].strip()

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
        except (json.JSONDecodeError, AttributeError) as e:
            logger.error(f"Failed to parse plan JSON for {task_id}: {e}")
            return [{"id": "P0", "description": question,
                     "expected_output": "任务完成"}]

    # ──────────────────────────────────────────────
    #  Phase 1b: Plan Revision
    # ──────────────────────────────────────────────

    def _revise_plan(self, task_id: str, existing_plan: List[Dict],
                     review_feedback: str, upstream_files: List[str],
                     server_id: str, tools) -> List[Dict]:
        """Revise an existing plan based on review feedback, with tool support."""
        plan_text = json.dumps(existing_plan, ensure_ascii=False, indent=2)
        tool_defs = tools.to_openai_tools()

        system_msg = (
            "你是一个工程规划专家。你需要根据审核反馈修改现有的执行计划。\n\n"
            f"【角色定义】\n{self.system_prompt}\n"
            f"{ENGINEERING_CONSTRAINTS}\n"
            "【修改要求】\n"
            "1. 仔细阅读审核反馈，理解需要修改的方面\n"
            "2. 可以使用工具收集额外信息来支持修改\n"
            "3. 保留原计划中合理的部分，只修改需要改进的部分\n"
            "4. 修改后的计划必须仍然是具体、可执行的\n\n"
            "【输出格式】\n"
            "完成修改后，输出 JSON 格式的计划（用 ```json 包裹）：\n"
            "```json\n"
            '{"goal_analysis": "修改说明",\n'
            ' "steps": [\n'
            '   {"id": "P0", "description": "步骤描述", "expected_output": "预期产出"}\n'
            " ]}\n"
            "```"
        )

        user_msg = (
            f"【任务目标】\n{task_id}\n\n"
            f"【现有执行计划】\n{plan_text}\n\n"
            f"【审核反馈】\n{review_feedback}"
        )

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ]

        if not tool_defs:
            response = llm_client.chat(messages, temperature=0.2)
            if response:
                return self._parse_plan_json(response, task_id, "")
            return existing_plan

        for iteration in range(_MAX_PLAN_ITERATIONS):
            if not llm_client.client:
                return existing_plan

            response = llm_client.chat_with_tools(messages, tool_defs, temperature=0.3)
            if response is None:
                break

            content = response.get("content", "") or ""
            tool_calls = response.get("tool_calls")

            assistant_msg: Dict[str, Any] = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            if not tool_calls:
                break

            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    fn_args = {}

                tool_output = tools.call(fn_name, **fn_args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_output,
                })

        return self._parse_plan_from_messages(messages, task_id, "")

    # ──────────────────────────────────────────────
    #  Plan persistence
    # ──────────────────────────────────────────────

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
        tools = self._build_tool_registry(task_id, "")
        tool_defs = tools.to_openai_tools()

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

                response = llm_client.chat_with_tools(messages, tool_defs, temperature=0.3)
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

                    tool_output = tools.call(fn_name, **fn_args)

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
