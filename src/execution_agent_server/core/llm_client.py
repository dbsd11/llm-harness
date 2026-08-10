# LLM Client - 封装大模型调用
import os
import json
from typing import Dict, Any, List, Optional
from openai import OpenAI
from logger import logger


class LLMClient:
    """
    大模型客户端封装

    支持阿里云百炼 DashScope API，使用 OpenAI 兼容接口
    """

    def __init__(self):
        self.api_key = os.getenv("DASHSCOPE_API_KEY")
        self.base_url = os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.model = os.getenv("LLM_MODEL", "qwen-plus")
        self.max_tokens = int(os.getenv("LLM_MAX_TOKENS", "4096"))
        self.enable_thinking = os.getenv("LLM_ENABLE_THINKING", "true").lower() == "true"
        self.timeout = int(os.getenv("LLM_TIMEOUT", "120"))  # 默认 120 秒超时

        if not self.api_key:
            logger.warning("DASHSCOPE_API_KEY not configured, LLM features disabled")
            self.client = None
        else:
            self.client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout
            )
            logger.info(f"LLM client initialized: model={self.model}, timeout={self.timeout}s")

    def chat(self, messages: List[Dict[str, str]], temperature: float = 0.7) -> Optional[str]:
        """
        发送聊天请求

        Args:
            messages: 消息列表，格式 [{"role": "user", "content": "..."}]
            temperature: 温度参数，控制随机性

        Returns:
            模型回复内容，失败返回 None
        """
        import time

        if not self.client:
            logger.error("LLM client not initialized")
            return None

        try:
            start_time = time.time()
            completion = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=self.max_tokens,
                extra_body={"enable_thinking": self.enable_thinking} if self.enable_thinking else None
            )
            elapsed_time = time.time() - start_time

            if completion.choices:
                content = completion.choices[0].message.content
                logger.info(f"LLM response received in {elapsed_time:.2f}s, length: {len(content)} chars")
                logger.debug(f"LLM response: {content[:100]}...")
                return content
            else:
                logger.warning(f"LLM returned empty response after {elapsed_time:.2f}s")
                return None

        except Exception as e:
            elapsed_time = time.time() - start_time if 'start_time' in locals() else 0
            logger.error(f"LLM call failed after {elapsed_time:.2f}s: {str(e)}")
            return None

    def decompose_goal(self, goal: str, context: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        """
        使用大模型智能分解目标为子任务

        Args:
            goal: 用户目标描述
            context: 上下文信息（优先级、超时等）

        Returns:
            子任务列表，每个子任务包含 goal, type, priority, timeout_seconds, context (含 role 和 system_prompt)
            失败返回 None
        """
        if not self.client:
            return None

        # 构造提示词
        # If the scenario configured execution-agent roles, constrain the LLM
        # to pick role names from that list so tasks associate + route correctly.
        role_names = context.get("execution_role_names") or []
        if role_names:
            names_str = "、".join(role_names)
            role_constraint = (
                f"5. **每个子任务的 context.role 必须从以下已配置的角色名称中选择，"
                f"不得自创名称**：{names_str}\n"
                f"   若目标只需单一角色，所有子任务的 role 都设为其中合适的一个。"
            )
        else:
            role_constraint = ""

        prompt = f"""你是一个任务分解专家。请将以下目标分解为多个子任务，每个子任务由一个专门角色的智能代理来完成。

目标：{goal}

上下文信息：
- 优先级：{context.get('priority', 0)}
- 超时时间：{context.get('timeout_seconds', 3600)}秒

重要要求：
1. 将目标分解为 2-5 个子任务
2. **每个子任务必须指定执行角色的专家代理**
3. 每个子任务的 context 中必须包含：
   - "role": 代理的角色/专长（如"数学家"、"数据分析师"、"编程专家"等）
   - "system_prompt": 系统提示词，定义该角色的能力和行为
   - "question": 该角色需要回答的具体问题或任务
4. 子任务之间应该有清晰的逻辑关系
5. **每个子任务必须有一个唯一的 id（如 "t1"、"t2"）和 depends_on 数组**：
   - "id": 本子任务的唯一标识（字符串）
   - "depends_on": 依赖的前序子任务 id 列表（数组）；无依赖用空数组 []
   - depends_on 只能引用同批次其他子任务的 id，必须构成**无环 DAG**（不得循环依赖，不得依赖自己）
   - 若子任务 B 需要子任务 A 的结果才能执行，则 B 的 depends_on 包含 "A 的 id"
   - 无依赖关系的子任务用空数组，它们会并行执行
{role_constraint}

示例：
如果目标是"编写代码计算1+1并执行它"，应该分解为：
- 子任务1: "编写计算1+1的Python代码"
  id: "t1", depends_on: []
  context: {{
    "role": "代码执行专家",
    "system_prompt": "...",
    "question": "编写计算1+1的Python代码"
  }}
- 子任务2: "执行上述代码并给出结果"
  id: "t2", depends_on: ["t1"]
  context: {{
    "role": "代码执行专家",
    "system_prompt": "...",
    "question": "执行上一步生成的代码并输出结果"
  }}

返回格式（严格遵循）：
```json
{{
  "subtasks": [
    {{
      "id": "t1",
      "goal": "子任务描述",
      "type": "execution",
      "priority": 0,
      "timeout_seconds": 3600,
      "depends_on": [],
      "context": {{
        "role": "角色名称",
        "system_prompt": "系统提示词",
        "question": "具体问题或任务"
      }}
    }}
  ]
}}
```

只返回 JSON，不要其他内容。"""

        messages = [
            {"role": "system", "content": "你是一个专业的任务分解助手，擅长将复杂目标分解为多个角色明确的子任务。"},
            {"role": "user", "content": prompt}
        ]

        try:
            import time
            start_time = time.time()
            response = self.chat(messages, temperature=0.3)
            elapsed_time = time.time() - start_time

            if not response:
                return None

            # 提取 JSON（可能被 markdown 代码块包裹）
            json_str = response
            if "```json" in response:
                json_str = response.split("```json")[1].split("```")[0].strip()
            elif "```" in response:
                json_str = response.split("```")[1].split("```")[0].strip()

            # 解析 JSON
            data = json.loads(json_str)
            subtasks = data.get("subtasks", [])

            # 验证和补充字段
            validated_subtasks = []
            # First pass: assign ids and collect the set of valid ids.
            valid_ids = set()
            for idx, task in enumerate(subtasks):
                if "goal" not in task:
                    continue
                tid = task.get("id") or f"t{idx + 1}"
                # de-duplicate ids
                if tid in valid_ids:
                    tid = f"t{idx + 1}"
                valid_ids.add(tid)

            for idx, task in enumerate(subtasks):
                if "goal" not in task:
                    continue
                tid = task.get("id") or f"t{idx + 1}"
                if tid not in valid_ids:
                    tid = f"t{idx + 1}"
                    valid_ids.add(tid)
                # Sanitize depends_on: drop unknown refs + self-refs.
                raw_deps = task.get("depends_on") or []
                if not isinstance(raw_deps, list):
                    raw_deps = []
                deps = [d for d in raw_deps
                        if d in valid_ids and d != tid]
                validated_task = {
                    "id": tid,
                    "goal": task["goal"],
                    "type": task.get("type", "execution"),
                    "priority": task.get("priority", context.get("priority", 0)),
                    "timeout_seconds": task.get("timeout_seconds", context.get("timeout_seconds", 3600)),
                    "depends_on": deps,
                    "context": task.get("context", {})
                }
                validated_subtasks.append(validated_task)

            if validated_subtasks:
                logger.info(f"LLM decomposed goal into {len(validated_subtasks)} subtasks in {elapsed_time:.2f}s")
                return validated_subtasks
            else:
                logger.warning(f"LLM returned empty subtask list after {elapsed_time:.2f}s")
                return None


        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM response as JSON: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"Goal decomposition failed: {str(e)}")
            return None

    def derive_followup_tasks(self, rejected_task: Dict[str, Any],
                              dependents: List[Dict[str, Any]],
                              context: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        """Re-derive the affected sub-tree rooted at a rejected task.

        Args:
          rejected_task: {goal, result (agent output), review_feedback, depends_on}
          dependents:    transitive PENDING dependents [{goal, context, depends_on}, ...]
          context:       scenario context (priority, timeout, agent_roles, etc.)
        Returns a fresh corrective sub-DAG in the same shape as decompose_goal
        ({id, goal, depends_on, context{role, system_prompt, question}, ...}):
          - root task(s) address the reviewer's feedback for the rejected task;
          - subsequent tasks re-derive the dependents, consuming the corrected output.
        Heuristic fallback (LLM unavailable): one retry subtask for the rejected task
        (same goal + feedback appended); dependents are re-pointed to it instead of re-derived.
        """
        rejected_goal = rejected_task.get("goal", "")
        rejected_output = rejected_task.get("result", "")
        review_feedback = rejected_task.get("review_feedback", "")
        rejected_deps = rejected_task.get("depends_on", [])

        # Heuristic fallback: one retry task for the rejected task
        def _fallback():
            retry_goal = f"{rejected_goal} (revised per reviewer feedback)"
            retry_task = {
                "id": "t1",
                "goal": retry_goal,
                "type": "execution",
                "priority": context.get("priority", 0),
                "timeout_seconds": context.get("timeout_seconds", 3600),
                "depends_on": rejected_deps,
                "context": {
                    "role": "通用助手",
                    "system_prompt": "你是一个有帮助的智能助手。",
                    "question": f"{rejected_goal}\n\nReviewer feedback: {review_feedback}",
                },
            }
            # Re-point dependents to the retry task
            result = [retry_task]
            for idx, dep in enumerate(dependents):
                dep_id = f"t{idx + 2}"
                dep_task = {
                    "id": dep_id,
                    "goal": dep.get("goal", ""),
                    "type": dep.get("type", "execution"),
                    "priority": dep.get("priority", context.get("priority", 0)),
                    "timeout_seconds": dep.get("timeout_seconds", context.get("timeout_seconds", 3600)),
                    "depends_on": ["t1"],  # depend on the retry task
                    "context": dep.get("context", {}),
                }
                result.append(dep_task)
            return result

        if not self.client:
            logger.warning("LLM not available for derive_followup_tasks, using heuristic fallback")
            return _fallback()

        # Build prompt for LLM
        dependent_strs = []
        for dep in dependents:
            dependent_strs.append(
                f"  - goal: {dep.get('goal', '')}\n"
                f"    depends_on: {dep.get('depends_on', [])}\n"
                f"    context: {dep.get('context', {})}"
            )
        dependents_text = "\n".join(dependent_strs) if dependent_strs else "(none)"

        # Role constraint (same as decompose_goal)
        role_names = context.get("execution_role_names") or []
        if role_names:
            names_str = "、".join(role_names)
            role_constraint = (
                f"5. **每个子任务的 context.role 必须从以下已配置的角色名称中选择，"
                f"不得自创名称**：{names_str}\n"
            )
        else:
            role_constraint = ""

        prompt = f"""你是一个任务修正专家。一个任务被人工审核拒绝，需要根据审核反馈重新生成修正方案。

## 被拒绝的任务
- 原始目标: {rejected_goal}
- Agent 输出: {rejected_output}
- 审核反馈: {review_feedback}
- 原始依赖: {rejected_deps}

## 受影响的下游任务（依赖被拒绝任务的后续任务）
{dependents_text}

## 要求
1. 根据审核反馈，为被拒绝的任务生成一个**修正后的根任务**，解决审核指出的问题
2. 为每个下游任务生成**修正后的版本**，确保它们能基于修正后的根任务输出正确执行
3. 保持与已完成的前序任务的依赖关系（{rejected_deps} 中的任务已成功）
4. 每个子任务必须有唯一 id（如 "t1", "t2"）和 depends_on 数组
5. 根任务的 depends_on 应该是被拒绝任务的原始 depends_on（已完成的前序任务）
{role_constraint}

返回格式（严格遵循）：
```json
{{
  "subtasks": [
    {{
      "id": "t1",
      "goal": "修正后的任务描述",
      "type": "execution",
      "priority": 0,
      "timeout_seconds": 3600,
      "depends_on": {json.dumps(rejected_deps)},
      "context": {{
        "role": "角色名称",
        "system_prompt": "系统提示词",
        "question": "具体问题或任务"
      }}
    }}
  ]
}}
```

只返回 JSON，不要其他内容。"""

        messages = [
            {"role": "system", "content": "你是一个专业的任务修正助手，擅长根据审核反馈重新设计任务方案。"},
            {"role": "user", "content": prompt}
        ]

        try:
            response = self.chat(messages, temperature=0.3)
            if not response:
                return _fallback()

            # Extract JSON
            json_str = response
            if "```json" in response:
                json_str = response.split("```json")[1].split("```")[0].strip()
            elif "```" in response:
                json_str = response.split("```")[1].split("```")[0].strip()

            data = json.loads(json_str)
            subtasks = data.get("subtasks", [])

            # Validate (reuse decompose_goal validation logic)
            validated = []
            valid_ids = set()
            for idx, task in enumerate(subtasks):
                if "goal" not in task:
                    continue
                tid = task.get("id") or f"t{idx + 1}"
                if tid in valid_ids:
                    tid = f"t{idx + 1}"
                valid_ids.add(tid)

            for idx, task in enumerate(subtasks):
                if "goal" not in task:
                    continue
                tid = task.get("id") or f"t{idx + 1}"
                if tid not in valid_ids:
                    tid = f"t{idx + 1}"
                    valid_ids.add(tid)
                raw_deps = task.get("depends_on") or []
                if not isinstance(raw_deps, list):
                    raw_deps = []
                deps = [d for d in raw_deps if d in valid_ids and d != tid]
                validated_task = {
                    "id": tid,
                    "goal": task["goal"],
                    "type": task.get("type", "execution"),
                    "priority": task.get("priority", context.get("priority", 0)),
                    "timeout_seconds": task.get("timeout_seconds", context.get("timeout_seconds", 3600)),
                    "depends_on": deps,
                    "context": task.get("context", {}),
                }
                validated.append(validated_task)

            if validated:
                logger.info(f"LLM derived {len(validated)} corrective subtasks for rejected task")
                return validated
            else:
                logger.warning("LLM returned empty corrective subtasks, using fallback")
                return _fallback()

        except Exception as e:
            logger.error(f"derive_followup_tasks failed: {e}, using fallback")
            return _fallback()


# 全局单例
llm_client = LLMClient()
