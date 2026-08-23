# Scheduling Agent - ReAct pattern for goal decomposition
import uuid
import json
import time
from typing import Dict, Any, List, Optional
from datetime import datetime
from .base_agent import BaseAgent
from .tool_registry import tool_registry
# Import tools module to trigger registration
import core.agents.scheduling_agent_tools
import core.agents.resource_repo_tool  # noqa: F401 — triggers tool registration
from core.state_machine import TaskState, TASK_STATE_MACHINE, TASK_EXECUTION_COMPLETE_STATES
from core.event_bus import event_bus
from core.message_queue import mqs, TaskMessage
from core.llm_client import llm_client
from database.repositories.task_repository import TaskRepository
from database.models.task import Task
from logger import logger


class SchedulingAgent(BaseAgent):
    """
    Scheduling Agent: ReAct pattern, idempotent control, task state machine

    Responsibilities:
    - Goal decomposition into tasks
    - Task scheduling and dependency management
    - Idempotent task execution (retry-safe)
    - State machine transitions
    - Tool invocation for execution server management
    - Assistant mode: plan review flow with assistant roles
    """

    _assistant_role_cache = {}  # (tenant_id, role_key) -> set of assistant role names

    def __init__(self):
        self.task_repo = TaskRepository()
        self.config = {}
        self.tools = tool_registry

    def get_agent_type(self) -> str:
        return "scheduling"

    def initialize(self, config: Dict[str, Any]) -> None:
        """Initialize scheduling agent with configuration"""
        self.config = config
        logger.info("SchedulingAgent initialized")

    def use_tool(self, tool_name: str, **kwargs) -> Dict[str, Any]:
        """
        Invoke a registered tool.

        Args:
            tool_name: Name of the tool to invoke
            **kwargs: Arguments to pass to the tool

        Returns:
            Tool result dict
        """
        try:
            if self.tenant_id:
                kwargs.setdefault("tenant_id", self.tenant_id)
            result = self.tools.call(tool_name, **kwargs)
            logger.info(f"Tool {tool_name} called successfully")
            return result
        except Exception as e:
            logger.error(f"Tool {tool_name} failed: {e}")
            return {"success": False, "error": str(e)}

    def run(self, task_id: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        ReAct loop:
        1. REASON: Analyze goal, plan tasks
        2. ACT: Create tasks, schedule execution
        3. OBSERVE: Monitor task progress
        4. REPEAT until goal achieved

        Args:
            task_id: Parent task ID (the goal)
            context: Goal context

        Returns:
            Scheduling result
        """
        goal = context.get("goal", "")
        if not goal:
            return {"success": False, "error": "No goal provided"}

        logger.info(f"SchedulingAgent processing goal: {goal}")

        self.tenant_id = context.get("tenant_id")
        manual_acceptance = context.get("manual_acceptance")
        resume_cycle = context.get("resume_cycle", False)
        scenario_id = context.get("scenario_id")

        # If manual_acceptance not in context, read from scenario config
        if manual_acceptance is None and scenario_id:
            try:
                from database.repositories.scenario_repository import ScenarioRepository
                scenario = ScenarioRepository().find_by_scenario_id(scenario_id)
                if scenario and scenario.config:
                    import json
                    scenario_config = json.loads(scenario.config)
                    manual_acceptance = scenario_config.get("manual_acceptance", False)
                else:
                    manual_acceptance = False
            except Exception as e:
                logger.warning(f"Failed to read scenario config for manual_acceptance: {e}")
                manual_acceptance = False

        try:
            # Detect assistant roles EARLY so we can exclude them from
            # the decomposition prompt's available role list.
            agent_roles_cfg = context.get("agent_roles") or {}
            configured_roles = agent_roles_cfg.get("execution_agents") or []
            assistant_roles = self._detect_assistant_roles(configured_roles)
            has_assistant = bool(assistant_roles)
            assistant_role_info = None
            if has_assistant:
                for role_cfg in configured_roles:
                    if role_cfg.get("name") in assistant_roles:
                        assistant_role_info = role_cfg
                        break
                logger.info(f"Assistant mode enabled: assistant roles={assistant_roles}")

            if resume_cycle and scenario_id:
                # Resume mode: load existing tasks, derive corrective tasks
                topic_id = str(uuid.uuid4())
                env_summary = self._collect_env_info()
                if env_summary:
                    context = dict(context)
                    context["execution_env"] = env_summary
                subtasks, local_id_to_task_id, created_by_id, subtask_ids = \
                    self._build_resume_plan(scenario_id, task_id, topic_id, context)
                if not subtasks:
                    return {"success": True, "topic_id": topic_id,
                            "subtask_ids": [], "subtask_count": 0}
                # Build waves from the remaining pending tasks
                try:
                    waves = self._build_waves(subtasks)
                except ValueError as cyc:
                    logger.error(f"SchedulingAgent: {cyc}")
                    return {"success": False, "error": str(cyc)}
            else:
                # Fresh cycle: decompose goal
                topic_id = str(uuid.uuid4())
                # Collect execution server environment info so the LLM can
                # make informed task assignments based on available tools/commands.
                env_summary = self._collect_env_info()
                if env_summary:
                    context = dict(context)
                    context["execution_env"] = env_summary
                # ReAct planning phase: LLM uses tools to gather system state
                planning_insights = self._planning_react_loop(goal, context, scenario_id)
                if planning_insights:
                    context = dict(context)
                    context["planning_insights"] = planning_insights
                    logger.info(f"Planning insights collected ({len(planning_insights)} chars)")
                logger.info(f"Starting goal decomposition for task {task_id}")
                context = dict(context)
                context["_assistant_roles"] = assistant_roles
                subtasks = self._decompose_goal(goal, context)
                subtasks = self._normalize_subtasks(subtasks)
                # Safety net: reassign any subtask that still got an assistant role
                exec_roles = [r for r in configured_roles if r.get("name") not in assistant_roles]
                if assistant_roles and exec_roles:
                    first_exec = exec_roles[0]
                    for sub in subtasks:
                        sub_role = (sub.get("context") or {}).get("role", "")
                        if sub_role in assistant_roles:
                            logger.warning(
                                f"Subtask '{sub.get('id')}' was assigned to assistant "
                                f"role '{sub_role}'; reassigning to '{first_exec.get('name')}'"
                            )
                            sub.setdefault("context", {})["role"] = first_exec.get("name", "")
                            sub["context"]["system_prompt"] = first_exec.get("role", "")
                logger.info(f"Goal decomposition completed: {len(subtasks)} subtasks created")

                local_id_to_task_id: Dict[str, str] = {}
                created_by_id: Dict[str, tuple] = {}
                for sub in subtasks:
                    lid = sub["id"]
                    deps = [d for d in (sub.get("depends_on") or [])]
                    subtask_id = self._create_subtask(
                        task_id, sub, topic_id=topic_id, scenario_id=scenario_id,
                        depends_on=deps, local_id_to_task_id=local_id_to_task_id,
                        context=context,
                    )
                    if subtask_id:
                        local_id_to_task_id[lid] = subtask_id
                        created_by_id[lid] = (subtask_id, sub)

                subtask_ids = list(local_id_to_task_id.values())

                try:
                    waves = self._build_waves(subtasks)
                except ValueError as cyc:
                    logger.error(f"SchedulingAgent: {cyc}")
                    return {"success": False, "error": str(cyc)}

            # DISPATCH + COLLECT per wave.
            if scenario_id and subtasks:
                agent_roles_cfg = context.get("agent_roles") or {}
                configured_roles = agent_roles_cfg.get("execution_agents") or []
                max_workers = context.get("max_workers", 3)
                total_timeout = context.get("timeout_seconds") or context.get("timeout", 300)
                deadline = time.time() + total_timeout
                all_replies: Dict[str, dict] = {}
                failed_ids: set = set()
                dispatched_ids: set = set()  # Track successfully dispatched tasks
                gated = False

                wave_idx = 0
                while wave_idx < len(waves):
                    wave = waves[wave_idx]
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        logger.warning(f"SchedulingAgent: timeout before wave {wave_idx}")
                        # Mark all pending tasks in remaining waves as failed
                        self._fail_unscheduled_tasks(waves, wave_idx, local_id_to_task_id,
                                                    "Scheduling timeout")
                        break

                    wave_msgs = []
                    wave_lids = []
                    for sub in wave:
                        tid = local_id_to_task_id.get(sub["id"]) or sub["id"]
                        if not tid:
                            continue
                        # Resolve deps: local_id -> real task_id, or pass-through real task_id
                        raw_deps = [d for d in (sub.get("depends_on") or [])]
                        resolved_deps = [local_id_to_task_id.get(d, d) for d in raw_deps]
                        # If any predecessor failed/skipped, this task is skipped.
                        if any(d in failed_ids for d in resolved_deps):
                            self.task_repo.mark_as_failed(
                                tid, "Skipped: predecessor task failed")
                            failed_ids.add(tid)
                            event_bus.emit("task.skipped", {
                                "task_id": tid, "reason": "predecessor failed"},
                                tenant_id=self.tenant_id)
                            continue

                        ctx = dict(sub.get("context", {}) or {})
                        role, sys_prompt, server_id = self._associate_role(
                            sub, configured_roles)
                        ctx["role"] = role
                        ctx["system_prompt"] = sys_prompt
                        if server_id:
                            ctx["server_id"] = server_id

                        # Inject assistant mode for non-assistant roles
                        if has_assistant and role not in assistant_roles:
                            ctx["assistant_mode"] = True
                            ctx["task_type"] = "generate_plan"

                        self._inject_upstream(ctx, resolved_deps, all_replies)
                        wave_msgs.append(TaskMessage(
                            task_id=tid, parent_task_id=task_id,
                            goal=sub.get("goal", ""), context=ctx,
                        ))
                        wave_lids.append(sub["id"])

                    if not wave_msgs:
                        continue

                    logger.info(f"SchedulingAgent: wave {wave_idx} dispatching "
                                f"{len(wave_msgs)} task(s) {[sub['id'] for sub in wave]}")

                    # Dispatch with status tracking
                    dispatch_status = mqs.dispatch_subtasks(scenario_id, wave_msgs, max_workers=max_workers)

                    # Handle dispatch failures
                    for msg in wave_msgs:
                        if dispatch_status.get(msg.task_id):
                            dispatched_ids.add(msg.task_id)
                        else:
                            # Dispatch failed - mark task as failed immediately
                            logger.error(f"Dispatch failed for task {msg.task_id}, marking as failed")
                            self.task_repo.mark_as_failed(
                                msg.task_id, "Failed to create dispatch message")
                            failed_ids.add(msg.task_id)
                            event_bus.emit("task.dispatch_failed", {
                                "task_id": msg.task_id,
                                "reason": "dispatch_message_creation_failed"
                            }, tenant_id=self.tenant_id)

                    if failed_ids:
                        self._propagate_failure(
                            failed_ids, waves, local_id_to_task_id, wave_idx + 1)

                    # Only collect replies for successfully dispatched tasks
                    wave_dispatched = [msg.task_id for msg in wave_msgs if dispatch_status.get(msg.task_id)]
                    if not wave_dispatched:
                        logger.warning(f"Wave {wave_idx}: no tasks successfully dispatched")
                        continue

                    wave_timeout = max(int(remaining), 1)
                    replies = mqs.collect_replies(
                        scenario_id, len(wave_dispatched), timeout=wave_timeout,
                        expected_task_ids=wave_dispatched)

                    for r in replies:
                        all_replies[r.task_id] = r.result
                        if r.pending_review:
                            gated = True
                        elif not r.success:
                            failed_ids.add(r.task_id)

                    # Handle plan_ready results: trigger plan review flow
                    if has_assistant and assistant_role_info:
                        plan_ready_tasks = []
                        for r in replies:
                            if r.result.get("phase") == "plan_ready":
                                plan_ready_tasks.append(r)

                        for pr in plan_ready_tasks:
                            review_result = self._handle_plan_review(
                                pr, assistant_role_info, scenario_id,
                                context, deadline, configured_roles)
                            if review_result is not None:
                                all_replies[pr.task_id] = review_result
                                if not review_result.get("success"):
                                    failed_ids.add(pr.task_id)

                    # Handle timeout - tasks that didn't reply in time
                    replied_ids = {r.task_id for r in replies}
                    missing_replies = set(wave_dispatched) - replied_ids
                    if missing_replies:
                        logger.warning(f"Wave {wave_idx}: {len(missing_replies)} tasks "
                                     f"did not reply within timeout: {missing_replies}")
                        for tid in missing_replies:
                            # Get subtask info for retry
                            subtask_info = None
                            for lid, (real_tid, sub) in created_by_id.items():
                                if real_tid == tid:
                                    subtask_info = sub
                                    break

                            # Try to retry or mark as failed
                            retried = self._retry_or_fail_task(
                                tid, "Task execution timeout - no reply received",
                                failed_ids, scenario_id, subtask_info, context
                            )

                            if not retried:
                                event_bus.emit("task.timeout", {
                                    "task_id": tid
                                }, tenant_id=self.tenant_id)
                                # Propagate timeout failures to dependents
                                self._propagate_failure(
                                    failed_ids, waves, local_id_to_task_id, wave_idx + 1)

                    # Manual acceptance: stop at gate after first wave with gated tasks
                    if manual_acceptance and gated:
                        logger.info(f"SchedulingAgent: manual_acceptance gate triggered, "
                                    f"pausing for review after wave {wave_idx}")
                        return {
                            "success": True,
                            "paused_for_review": True,
                            "topic_id": topic_id,
                            "subtask_ids": subtask_ids,
                            "replies": all_replies,
                        }

                    # Heuristic check: evaluate results and regenerate DAG if needed
                    new_waves = self._heuristic_wave_check(
                        wave_idx=wave_idx, waves=waves,
                        wave_dispatched=wave_dispatched, replies=replies,
                        all_replies=all_replies, failed_ids=failed_ids,
                        local_id_to_task_id=local_id_to_task_id,
                        created_by_id=created_by_id,
                        context=context, configured_roles=configured_roles,
                        assistant_roles=assistant_roles,
                    )
                    if new_waves is not None:
                        waves = waves[:wave_idx + 1] + new_waves
                        logger.info(f"Waves updated: {len(waves)} total "
                                    f"({wave_idx + 1} done + {len(new_waves)} new)")

                    wave_idx += 1

                event_bus.emit("task.scheduled", {
                    "task_id": task_id,
                    "topic_id": topic_id,
                    "subtask_count": len(subtask_ids),
                    "subtask_ids": subtask_ids,
                }, tenant_id=self.tenant_id)

                return {
                    "success": True,
                    "topic_id": topic_id,
                    "subtask_ids": subtask_ids,
                    "subtask_count": len(subtask_ids),
                    "replies": all_replies,
                }

            # OBSERVE: Return scheduling result (no scenario_id — skip dispatch)
            event_bus.emit("task.scheduled", {
                "task_id": task_id,
                "topic_id": topic_id,
                "subtask_count": len(subtask_ids),
                "subtask_ids": subtask_ids,
            }, tenant_id=self.tenant_id)

            return {
                "success": True,
                "topic_id": topic_id,
                "subtask_ids": subtask_ids,
                "subtask_count": len(subtask_ids),
            }

        except Exception as e:
            error_msg = str(e)
            logger.error(f"SchedulingAgent error: {error_msg}")

            event_bus.emit("task.scheduling_failed", {
                "task_id": task_id,
                "error": error_msg,
            }, tenant_id=self.tenant_id)

            return {
                "success": False,
                "error": error_msg,
            }

    def _calculate_task_timeout(self, subtask: Dict[str, Any], context: Dict[str, Any]) -> int:
        """Calculate dynamic timeout for a task based on its complexity and type.

        Strategy:
        - Base timeout from context (default 3600s)
        - Adjust based on task goal keywords (environment setup, code analysis, etc.)
        - Add buffer for complex tasks (multiple dependencies, large scope)

        Returns timeout in seconds.
        """
        base_timeout = context.get("timeout_seconds") or context.get("timeout", 3600)
        goal = subtask.get("goal", "").lower()

        # Environment setup tasks - typically fast
        if any(keyword in goal for keyword in ["环境准备", "代码获取", "git clone", "安装"]):
            return min(600, base_timeout)  # 10 minutes max

        # Code analysis tasks - medium complexity
        if any(keyword in goal for keyword in ["分析", "解析", "结构", "架构", "模块"]):
            # Check if it's a comprehensive analysis
            if any(keyword in goal for keyword in ["完整", "全面", "深度", "详细"]):
                return min(900, base_timeout)  # 15 minutes for comprehensive analysis
            return min(600, base_timeout)  # 10 minutes for basic analysis

        # Report generation tasks - depend on upstream results
        if any(keyword in goal for keyword in ["报告", "总结", "汇总"]):
            # More time for report generation as it needs to process upstream results
            return min(1200, base_timeout)  # 20 minutes

        # Default: use base timeout but cap at reasonable limit
        return min(base_timeout, 3600)

    def _retry_or_fail_task(self, task_id: str, error_msg: str, failed_ids: set,
                           scenario_id: str, subtask: Dict[str, Any] = None,
                           context: Dict[str, Any] = None) -> bool:
        """Attempt to retry a failed task, or mark it as failed if retries exhausted.

        Args:
            task_id: Task ID to retry/fail
            error_msg: Error message to record
            failed_ids: Set to add failed task IDs to
            scenario_id: Scenario ID for re-dispatch
            subtask: Original subtask definition (for re-dispatch)
            context: Task context (for re-dispatch)

        Returns:
            True if task was retried, False if marked as failed
        """
        task = self.task_repo.find_by_task_id(task_id)
        if not task:
            logger.error(f"Task {task_id} not found for retry")
            self.task_repo.mark_as_failed(task_id, error_msg)
            failed_ids.add(task_id)
            return False

        # State-aware guard: skip retry if task already reached a terminal state
        # (original execution completed after timeout) or is still running
        # (original execution may still produce a result).
        current_state = task.state
        if current_state in TASK_EXECUTION_COMPLETE_STATES:
            logger.info(f"Task {task_id} already terminal ({current_state}), "
                        f"skipping retry")
            if current_state in ('failed', 'timeout', 'cancelled'):
                failed_ids.add(task_id)
            return False
        if current_state == 'running':
            logger.warning(f"Task {task_id} is still running, skipping retry "
                           f"to avoid double dispatch")
            return False

        # Check if retries are exhausted
        max_retries = task.max_retries or 3
        current_retries = task.retry_count or 0

        if current_retries >= max_retries:
            logger.warning(f"Task {task_id} exhausted {max_retries} retries, marking as failed")
            self.task_repo.mark_as_failed(task_id, f"{error_msg} (after {max_retries} retries)")
            failed_ids.add(task_id)
            event_bus.emit("task.failed", {
                "task_id": task_id,
                "error": error_msg,
                "retries_exhausted": True,
            }, tenant_id=self.tenant_id)
            return False

        # Increment retry count and reset task to pending
        new_retry_count = current_retries + 1
        logger.info(f"Retrying task {task_id} (attempt {new_retry_count}/{max_retries})")

        # Update task state back to pending and increment retry count
        self.task_repo.increment_retry_count(task_id)
        task.state = TaskState.PENDING.value
        task.error = None
        task.updated_at = datetime.now()
        self.task_repo.update(task)

        # Re-dispatch the task
        if subtask and scenario_id:
            from core.message_queue import mqs, TaskMessage

            # Recalculate timeout with exponential backoff
            base_timeout = self._calculate_task_timeout(subtask, context) if context else 3600
            retry_timeout = int(base_timeout * (1.5 ** new_retry_count))  # Exponential backoff

            # Update task timeout
            task.timeout_seconds = retry_timeout
            self.task_repo.update(task)

            # Create new dispatch message
            ctx = dict(subtask.get("context", {}) or {})
            ctx["retry_attempt"] = new_retry_count

            msg = TaskMessage(
                task_id=task_id,
                parent_task_id=task.parent_task_id,
                goal=task.goal,
                context=ctx,
            )

            mqs.dispatch_subtasks(scenario_id, [msg], max_workers=1)

            event_bus.emit("task.retried", {
                "task_id": task_id,
                "retry_count": new_retry_count,
                "error": error_msg,
            }, tenant_id=self.tenant_id)

            return True
        else:
            # Cannot retry without subtask info, mark as failed
            logger.warning(f"Cannot retry task {task_id} without subtask info, marking as failed")
            self.task_repo.mark_as_failed(task_id, f"{error_msg} (no retry info)")
            failed_ids.add(task_id)
            return False

    def _associate_role(self, subtask: Dict[str, Any],
                        configured_roles: List[Dict[str, Any]]):
        """Associate a subtask with a configured execution-agent role.

        Returns (role_name, system_prompt, server_id):
        - If the subtask's role exactly matches a configured role name, use
          that role's configured definition (system prompt) + server_id.
        - If no match but roles are configured, fall back to the FIRST
          configured role (with a warning) so the task still routes and
          executes — an unassociated task cannot execute correctly.
        - If no roles are configured, pass through the LLM's role/system_prompt
          with server_id=None (local execution, the default behavior).
        """
        ctx = subtask.get("context", {}) or {}
        role = ctx.get("role", "")

        if configured_roles:
            for ag in configured_roles:
                if ag.get("name") == role:
                    return (ag.get("name", role),
                            ag.get("role", ""),
                            ag.get("server_id"))
            # No exact match — the LLM invented a name. Fall back to the first
            # configured role so the task is still associated + routable.
            first = configured_roles[0]
            logger.warning(
                f"Subtask role '{role}' not in configured roles "
                f"{[r.get('name') for r in configured_roles]}; "
                f"assigning '{first.get('name')}'"
            )
            return (first.get("name", role),
                    first.get("role", ""),
                    first.get("server_id"))

        # No configured roles -> pass through LLM values, execute locally.
        return role, ctx.get("system_prompt", ""), None

    # --- dependency / wave scheduling ---------------------------------------

    def _normalize_subtasks(self, subtasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Ensure each subtask has a unique id + a depends_on list.

        Heuristic decomposition or older LLM output may omit these; without an
        id they can't participate in the dependency graph. Missing -> all land
        in wave 0 (parallel), preserving the legacy behavior.
        """
        seen = set()
        normalized = []
        for idx, sub in enumerate(subtasks):
            s = dict(sub)
            tid = s.get("id") or f"t{idx + 1}"
            if tid in seen:
                tid = f"t{idx + 1}"
            seen.add(tid)
            s["id"] = tid
            deps = s.get("depends_on") or []
            if not isinstance(deps, list):
                deps = []
            s["depends_on"] = deps
            normalized.append(s)
        return normalized

    def _build_waves(self, subtasks: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """Group subtasks into topological execution waves.

        Wave 0 = subtasks with no (valid) dependencies. Wave N = subtasks all
        of whose dependencies are in earlier waves. Tasks within a wave run in
        parallel; waves run serially so a dependent task can consume its
        predecessors' results.

        Drops unknown/self dependency refs. Raises ValueError on a cycle.
        """
        by_id = {s["id"]: s for s in subtasks}
        # Sanitize depends_on to known, non-self refs.
        deps = {
            s["id"]: [d for d in (s.get("depends_on") or []) if d in by_id and d != s["id"]]
            for s in subtasks
        }
        waves: List[List[Dict[str, Any]]] = []
        placed: set = set()
        remaining = set(by_id.keys())

        while remaining:
            ready = [sid for sid in remaining if all(d in placed for d in deps[sid])]
            if not ready:
                raise ValueError(
                    f"Dependency cycle detected among: {sorted(remaining)}"
                )
            waves.append([by_id[sid] for sid in ready])
            placed.update(ready)
            remaining -= set(ready)
        return waves

    def _inject_upstream(self, ctx: Dict[str, Any], resolved_dep_ids: List[str],
                         replies_by_task_id: Dict[str, dict]) -> None:
        """Inject predecessor results into a dependent task's context.

        Adds ctx["upstream_results"] = {real_task_id: reply_result} and
        ctx["upstream_outputs"] = [result["output"]...] in dependency order.
        resolved_dep_ids must contain real task_ids (already mapped from local ids).
        No-op when resolved_dep_ids is empty.
        """
        if not resolved_dep_ids:
            return
        upstream = {}
        outputs = []
        for tid in resolved_dep_ids:
            if tid not in replies_by_task_id:
                continue
            res = replies_by_task_id[tid]
            upstream[tid] = res
            outputs.append(res.get("output", ""))
        ctx["upstream_results"] = upstream
        ctx["upstream_outputs"] = outputs

    def _propagate_failure(self, failed_ids: set, waves: List[List[Dict[str, Any]]],
                           local_id_to_task_id: Dict[str, str],
                           start_wave: int) -> set:
        """Mark all transitive dependents of failed_ids in later waves as
        skipped (failed). Returns the set of skipped task_ids."""
        skipped = set()
        for wave in waves[start_wave:]:
            for sub in wave:
                raw_deps = [d for d in (sub.get("depends_on") or [])]
                resolved_deps = [local_id_to_task_id.get(d, d) for d in raw_deps]
                failed_or_skipped = failed_ids | skipped
                if any(d in failed_or_skipped for d in resolved_deps):
                    tid = local_id_to_task_id.get(sub["id"]) or sub["id"]
                    if tid:
                        try:
                            self.task_repo.mark_as_failed(
                                tid, "Skipped: predecessor task failed")
                        except Exception as e:
                            logger.error(f"Failed to mark skipped task {tid}: {e}")
                        event_bus.emit("task.skipped", {
                            "task_id": tid, "reason": "predecessor failed",
                        }, tenant_id=self.tenant_id)
                        skipped.add(tid)
        return skipped

    def _fail_unscheduled_tasks(self, waves: List[List[Dict[str, Any]]],
                                start_wave: int, local_id_to_task_id: Dict[str, str],
                                reason: str) -> set:
        """Mark all tasks in waves from start_wave onwards as failed.

        Used when scheduling times out before all waves are dispatched.
        Returns the set of failed task_ids.
        """
        failed = set()
        for wave in waves[start_wave:]:
            for sub in wave:
                tid = local_id_to_task_id.get(sub["id"]) or sub["id"]
                if tid:
                    try:
                        self.task_repo.mark_as_failed(tid, reason)
                        failed.add(tid)
                        event_bus.emit("task.unscheduled", {
                            "task_id": tid, "reason": reason,
                        }, tenant_id=self.tenant_id)
                    except Exception as e:
                        logger.error(f"Failed to mark unscheduled task {tid}: {e}")
        if failed:
            logger.warning(f"Marked {len(failed)} tasks as failed due to: {reason}")
        return failed

    def _build_resume_plan(self, scenario_id: str, parent_task_id: str,
                           topic_id: str, context: Dict[str, Any]):
        """Build a resume plan for cycle N > 1 (manual_acceptance resume).

        Loads all tasks for the scenario, identifies rejected tasks (failed + review_feedback),
        builds affected sub-trees, derives corrective tasks, cancels old PENDING dependents,
        and returns the next dispatchable wave.

        Returns:
            (subtasks, local_id_to_task_id, created_by_id, subtask_ids)
        """
        from collections import deque

        all_tasks = self.task_repo.find_by_scenario_id(scenario_id)
        task_map = {t.task_id: t for t in all_tasks}

        # Identify rejected tasks (failed + review_feedback)
        rejected = [t for t in all_tasks
                    if t.state == "failed" and t.review_feedback]

        # Build affected sub-trees: for each rejected task, compute transitive dependents
        affected_subtrees = []  # list of (rejected_task, [dependent_tasks])
        cancelled_ids = set()  # task_ids to cancel (old PENDING dependents)

        for rej_task in rejected:
            # Reverse BFS from rejected task over PENDING tasks
            dependents = []
            queue = deque([rej_task.task_id])
            visited = {rej_task.task_id}
            while queue:
                current_id = queue.popleft()
                deps = self.task_repo.find_dependents_by_task_id(current_id)
                for dep in deps:
                    if dep.task_id not in visited and dep.state == "pending":
                        visited.add(dep.task_id)
                        dependents.append(dep)
                        queue.append(dep.task_id)

            affected_subtrees.append((rej_task, dependents))
            for dep in dependents:
                cancelled_ids.add(dep.task_id)

        # Cancel old PENDING dependents
        for tid in cancelled_ids:
            self.task_repo.mark_as_cancelled(tid, "Superseded by re-derivation")

        # Derive corrective tasks for each affected sub-tree
        subtasks = []
        local_id_to_task_id = {}
        created_by_id = {}
        subtask_ids = []

        for rej_task, dependents in affected_subtrees:
            rej_info = {
                "goal": rej_task.goal,
                "result": rej_task.result or "",
                "review_feedback": rej_task.review_feedback or "",
                "depends_on": json.loads(rej_task.depends_on) if rej_task.depends_on else [],
            }
            dep_infos = []
            for dep in dependents:
                dep_infos.append({
                    "goal": dep.goal,
                    "context": json.loads(dep.context) if dep.context else {},
                    "depends_on": json.loads(dep.depends_on) if dep.depends_on else [],
                })

            corrective_subtasks = llm_client.derive_followup_tasks(
                rej_info, dep_infos, context)

            if corrective_subtasks:
                for sub in corrective_subtasks:
                    lid = sub["id"]
                    deps = [d for d in (sub.get("depends_on") or [])]
                    subtask_id = self._create_subtask(
                        parent_task_id, sub, topic_id=topic_id, scenario_id=scenario_id,
                        depends_on=deps, local_id_to_task_id=local_id_to_task_id,
                        context=context,
                    )
                    if subtask_id:
                        created_by_id[lid] = (subtask_id, sub)
                        subtask_ids.append(subtask_id)

        # Build remapping: local_id -> real task_id (from _create_subtask side-effect)
        local_to_real = {}
        for lid, (real_tid, _) in created_by_id.items():
            local_to_real[lid] = real_tid

        # Add corrective subtasks with real task_ids and resolved depends_on
        for lid, (real_tid, sub) in created_by_id.items():
            raw_deps = sub.get("depends_on") or []
            resolved_deps = [local_to_real.get(d, d) for d in raw_deps]
            subtasks.append({
                "id": real_tid,
                "goal": sub.get("goal", ""),
                "type": sub.get("type", "execution"),
                "depends_on": resolved_deps,
                "context": sub.get("context", {}),
            })

        # Collect remaining PENDING tasks (not cancelled, dispatchable)
        for t in all_tasks:
            if t.state == "pending" and t.task_id not in cancelled_ids:
                deps = json.loads(t.depends_on) if t.depends_on else []
                all_deps_success = all(
                    task_map.get(d) and task_map[d].state == "success"
                    for d in deps
                ) if deps else True
                if all_deps_success:
                    subtasks.append({
                        "id": t.task_id,
                        "goal": t.goal,
                        "type": "execution",
                        "depends_on": deps,
                        "context": json.loads(t.context) if t.context else {},
                    })

        return subtasks, local_id_to_task_id, created_by_id, subtask_ids

    def _collect_env_info(self) -> List[Dict[str, Any]]:
        """Query connected execution servers and return their env_info summaries.

        Returns a list of {server_id, name, env_info} for each connected server.
        Returns [] when no servers are connected or on error.
        """
        try:
            from database.repositories.execution_server_repository import ExecutionServerRepository
            repo = ExecutionServerRepository()
            if not self.tenant_id:
                logger.warning("_collect_env_info: tenant_id not set, returning empty server list")
                return []
            servers = repo.find_all_by_tenant(self.tenant_id)
            result = []
            for s in servers:
                if not s.connected:
                    continue
                env_raw = s.env_info
                if not env_raw or env_raw == "{}":
                    continue
                env = json.loads(env_raw) if isinstance(env_raw, str) else env_raw
                result.append({
                    "server_id": s.server_id,
                    "name": s.name,
                    "env_info": env,
                })
            if result:
                logger.info(f"Collected env_info from {len(result)} connected execution server(s)")
            return result
        except Exception as e:
            logger.warning(f"Failed to collect execution server env_info: {e}")
            return []

    _MAX_PLANNING_ITERATIONS = 5

    def _planning_react_loop(self, goal: str, context: Dict[str, Any],
                              scenario_id: Optional[str] = None) -> Optional[str]:
        """Run a bounded ReAct loop to gather context via tools before decomposition.

        The LLM can call registered tools (list servers, query tasks, etc.) to
        understand the current system state. Returns the LLM's planning summary
        string, or None if planning is skipped (no tools / LLM unavailable).

        Args:
            goal: The user's goal description
            context: Goal context (priority, timeout, agent_roles, etc.)
            scenario_id: Optional scenario ID for context

        Returns:
            Planning insights string, or None
        """
        tools = self.tools.to_openai_tools()
        if not tools:
            logger.debug("No tools registered, skipping planning phase")
            return None

        if not llm_client.client:
            logger.debug("LLM not configured, skipping planning phase")
            return None

        system_msg = (
            "你是一个任务规划助手。在分解用户目标之前，你可以使用工具查询当前系统状态，"
            "包括可用的执行服务器、已有任务、服务器环境信息等。\n"
            "请根据需要调用工具收集信息，然后给出一段简明的规划建议，帮助后续的任务分解。\n"
            "如果你认为已有信息足够，可以直接输出规划建议而不调用工具。"
        )

        user_parts = [f"用户目标：{goal}"]
        if scenario_id:
            user_parts.append(f"场景 ID：{scenario_id}")
        priority = context.get("priority", 0)
        timeout = context.get("timeout_seconds") or context.get("timeout", 3600)
        user_parts.append(f"优先级：{priority}，超时：{timeout}s")

        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": "\n".join(user_parts)},
        ]

        for iteration in range(self._MAX_PLANNING_ITERATIONS):
            try:
                response = llm_client.chat_with_tools(messages, tools, temperature=0.3)
            except Exception as e:
                logger.warning(f"Planning ReAct LLM call failed at iteration {iteration}: {e}")
                return None

            if response is None:
                logger.warning("Planning ReAct: LLM returned empty response")
                return None

            content = response.get("content", "")
            tool_calls = response.get("tool_calls")

            # Build assistant message for history
            assistant_msg: Dict[str, Any] = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            if not tool_calls:
                logger.info(f"Planning ReAct completed after {iteration + 1} iteration(s)")
                return content or None

            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    fn_args = {}

                try:
                    if self.tenant_id:
                        fn_args.setdefault("tenant_id", self.tenant_id)
                    tool_result = self.tools.call(fn_name, **fn_args)
                    tool_output = json.dumps(tool_result, ensure_ascii=False, default=str)
                except Exception as e:
                    tool_output = json.dumps({"error": str(e)})
                    logger.warning(f"Planning tool {fn_name} failed: {e}")

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_output,
                })

        logger.warning(f"Planning ReAct hit max iterations ({self._MAX_PLANNING_ITERATIONS})")
        return content or None

    def _decompose_goal(self, goal: str, context: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Decompose goal into subtasks (子主题).

        Strategy:
        1. Try LLM-based intelligent decomposition first
        2. Fall back to rule-based heuristic if LLM fails or not configured

        Returns:
            List of subtask dicts
        """
        priority = context.get("priority", 0)
        timeout = context.get("timeout_seconds") or context.get("timeout", 3600)
        exec_context = context.get("execution_context", {})

        logger.info(f"Attempting goal decomposition for: {goal[:100]}...")

        # Pass configured execution-agent role names to the LLM so it picks
        # from them (rather than inventing names that won't route).
        configured_roles = (context.get("agent_roles") or {}).get(
            "execution_agents", []) or []
        assistant_roles = context.get("_assistant_roles") or set()
        if configured_roles:
            exec_role_names = [
                r.get("name", "") for r in configured_roles
                if r.get("name", "") not in assistant_roles
            ]
            context = dict(context)
            context["execution_role_names"] = exec_role_names
            if assistant_roles:
                context["assistant_role_names"] = list(assistant_roles)

        # 优先使用大模型进行智能分解
        if llm_client.client:
            try:
                logger.info("Calling LLM for goal decomposition...")
                llm_subtasks = llm_client.decompose_goal(goal, context)
                if llm_subtasks:
                    logger.info(f"LLM successfully decomposed goal into {len(llm_subtasks)} subtasks")
                    return llm_subtasks
                else:
                    logger.warning("LLM returned None or empty subtasks, falling back to heuristic")
            except Exception as e:
                logger.warning(f"LLM decomposition failed, falling back to heuristic: {str(e)}")
        else:
            logger.info("LLM client not configured, using heuristic decomposition")

        # 回退到基于规则的启发式分解
        logger.info("Using rule-based heuristic for goal decomposition")

        # Heuristic: if goal contains "and", split into multiple subtasks
        lower_goal = goal.lower()
        if " and " in lower_goal:
            parts = [p.strip() for p in goal.split(" and ") if p.strip()]
            if len(parts) > 1:
                return [
                    {
                        "goal": part,
                        "type": "execution",
                        "priority": priority,
                        "timeout_seconds": timeout,
                        "context": exec_context,
                    }
                    for part in parts
                ]

        # Default: create analysis + execution subtasks
        return [
            {
                "goal": f"Analyze: {goal}",
                "type": "execution",
                "priority": priority,
                "timeout_seconds": min(60, timeout),
                "context": {"command": f"echo 'Analyzing: {goal}'"},
            },
            {
                "goal": f"Execute: {goal}",
                "type": "execution",
                "priority": priority,
                "timeout_seconds": timeout,
                "context": exec_context,
            },
        ]

    def _create_subtask(self, parent_task_id: str, subtask: Dict[str, Any],
                        topic_id: str = None,
                        scenario_id: str = None,
                        depends_on: List[str] = None,
                        local_id_to_task_id: Dict[str, str] = None,
                        context: Dict[str, Any] = None) -> Optional[str]:
        """
        Create a subtask with idempotency control (幂等控制).

        If a task with the same idempotency_key already exists, returns
        the existing task_id instead of creating a duplicate.

        Args:
            depends_on: local predecessor ids (e.g. ["t1"])
            local_id_to_task_id: map to resolve local ids -> real task_ids
                for persistence. The new task's own id is registered here.
            context: task context for dynamic timeout calculation

        Returns:
            task_id if created/found, None if error
        """
        goal = subtask.get("goal", "")
        idempotency_key = f"{parent_task_id}:{goal}"

        # Idempotency check: return existing if already created
        existing = self.task_repo.find_by_idempotency_key(idempotency_key)
        if existing:
            logger.info(f"Subtask already exists (idempotent): {idempotency_key}")
            if local_id_to_task_id is not None and subtask.get("id"):
                local_id_to_task_id[subtask["id"]] = existing.task_id
            return existing.task_id

        task_id = str(uuid.uuid4())
        # Resolve local predecessor ids to real task_ids for persistence.
        resolved_deps = []
        if depends_on and local_id_to_task_id:
            resolved_deps = [local_id_to_task_id[d] for d in depends_on
                             if d in local_id_to_task_id]

        # Calculate dynamic timeout based on task type
        dynamic_timeout = subtask.get("timeout_seconds")
        if dynamic_timeout is None and context:
            dynamic_timeout = self._calculate_task_timeout(subtask, context)
        elif dynamic_timeout is None:
            dynamic_timeout = 3600  # default fallback

        task = Task(
            task_id=task_id,
            parent_task_id=parent_task_id,
            topic_id=topic_id,
            scenario_id=scenario_id,
            idempotency_key=idempotency_key,
            depends_on=json.dumps(resolved_deps, ensure_ascii=False) if resolved_deps else None,
            goal=goal,
            state=TaskState.PENDING.value,
            priority=subtask.get("priority", 0),
            timeout_seconds=dynamic_timeout,
            max_retries=3,
            retry_count=0,
            tenant_id=self.tenant_id,
            context=json.dumps(subtask.get("context", {}), ensure_ascii=False),
            created_at=datetime.now(),
            updated_at=datetime.now()
        )

        self.task_repo.create(task)

        if local_id_to_task_id is not None and subtask.get("id"):
            local_id_to_task_id[subtask["id"]] = task_id

        event_bus.emit("task.created", {
            "task_id": task_id,
            "parent_task_id": parent_task_id,
            "topic_id": topic_id,
            "goal": goal,
            "depends_on": resolved_deps,
        }, tenant_id=self.tenant_id)

        logger.info(f"Created subtask {task_id} for parent {parent_task_id} "
                    f"(topic: {topic_id}, depends_on: {resolved_deps})")
        return task_id

    def create_task(self, goal: str, parent_task_id: Optional[str] = None,
                   context: Dict[str, Any] = None) -> str:
        """
        Create a new task.

        Args:
            goal: Task goal
            parent_task_id: Parent task ID (for hierarchies)
            context: Task context

        Returns:
            Task ID
        """
        task_id = str(uuid.uuid4())

        task = Task(
            task_id=task_id,
            parent_task_id=parent_task_id,
            goal=goal,
            state=TaskState.PENDING.value,
            priority=context.get("priority", 0) if context else 0,
            timeout_seconds=(context.get("timeout_seconds") or context.get("timeout", 3600)) if context else 3600,
            max_retries=3,
            retry_count=0,
            tenant_id=self.tenant_id,
            context=json.dumps(context, ensure_ascii=False) if context else "{}",
            created_at=datetime.now(),
            updated_at=datetime.now()
        )

        self.task_repo.create(task)

        event_bus.emit("task.created", {
            "task_id": task_id,
            "goal": goal,
        }, tenant_id=self.tenant_id)

        return task_id

    def transition_task_state(self, task_id: str, new_state: TaskState) -> bool:
        """
        Transition task state machine.

        Args:
            task_id: Task ID
            new_state: New state

        Returns:
            True if transition succeeded
        """
        task = self.task_repo.find_by_task_id(task_id)
        if not task:
            logger.error(f"Task not found: {task_id}")
            return False

        # Validate transition
        sm = TASK_STATE_MACHINE
        sm.initialize(TaskState(task.state))

        if not sm.can_transition(new_state):
            logger.error(f"Invalid state transition: {task.state} -> {new_state.value}")
            return False

        # Perform transition
        old_state = task.state
        self.task_repo.update_task_state(task_id, new_state.value)

        event_bus.emit("task.state_changed", {
            "task_id": task_id,
            "old_state": old_state,
            "new_state": new_state.value,
        }, tenant_id=self.tenant_id)

        logger.info(f"Task {task_id} state: {old_state} -> {new_state.value}")
        return True

    def cleanup(self) -> None:
        """Cleanup resources"""
        logger.info("SchedulingAgent cleaned up")

    # ──────────────────────────────────────────────
    #  Assistant mode support
    # ──────────────────────────────────────────────

    def _detect_assistant_roles(self,
                                 configured_roles: List[Dict[str, Any]]) -> set:
        """Use LLM to analyze role descriptions and identify assistant roles.

        An assistant role is one whose primary function is reviewing plans,
        providing feedback, or supplementary information — rather than direct
        task execution.

        Results are cached to avoid repeated LLM calls.
        Returns set of role names that are assistants.
        """
        if not configured_roles:
            return set()

        role_key = "|".join(sorted(
            f"{r.get('name', '')}:{r.get('role', '')[:50]}"
            for r in configured_roles
        ))
        cache_key = (self.tenant_id or "", role_key)

        if cache_key in self._assistant_role_cache:
            return self._assistant_role_cache[cache_key]

        if not llm_client.client:
            logger.debug("LLM not configured, skipping assistant role detection")
            self._assistant_role_cache[cache_key] = set()
            return set()

        roles_desc = "\n".join(
            f"- 角色名称: {r.get('name', 'unknown')}\n"
            f"  角色定义: {r.get('role', '未定义')}"
            for r in configured_roles
        )

        prompt = (
            "你是一个角色分类专家。请分析以下执行代理角色，判断哪些是'助手'角色。\n\n"
            "【助手角色的特征】\n"
            "1. 主要职责是审核计划、提供反馈、补充信息\n"
            "2. 角色定义中包含'审核'、'评审'、'反馈'、'建议'、'人工'、'确认'等关键词\n"
            "3. 不直接执行具体任务（如编码、测试、部署），而是辅助其他角色\n"
            "4. 角色名称中包含'助手'、'审核员'、'评审'、'human'等\n\n"
            "【执行代理角色列表】\n"
            f"{roles_desc}\n\n"
            "请分析每个角色，判断是否为助手角色。\n"
            "返回格式（严格遵循）：\n"
            '```json\n{"assistant_roles": ["角色名称1", "角色名称2"]}\n```\n'
            "如果没有助手角色，返回空数组。只返回 JSON。"
        )

        try:
            messages = [
                {"role": "system", "content": "你是角色分类助手，只输出 JSON。"},
                {"role": "user", "content": prompt},
            ]
            response = llm_client.chat(messages, temperature=0.1)
            if not response:
                self._assistant_role_cache[cache_key] = set()
                return set()

            json_str = response
            if "```json" in response:
                json_str = response.split("```json")[1].split("```")[0].strip()
            elif "```" in response:
                json_str = response.split("```")[1].split("```")[0].strip()

            data = json.loads(json_str)
            assistant_names = set(data.get("assistant_roles", []))

            valid_names = {r.get("name") for r in configured_roles}
            assistant_names &= valid_names

            if assistant_names:
                logger.info(f"Detected assistant roles: {assistant_names}")

            self._assistant_role_cache[cache_key] = assistant_names
            return assistant_names

        except Exception as e:
            logger.warning(f"Assistant role detection failed: {e}")
            self._assistant_role_cache[cache_key] = set()
            return set()

    def _handle_plan_review(self, plan_result, assistant_role_info: Dict,
                             scenario_id: str, context: Dict[str, Any],
                             deadline: float,
                             configured_roles: List[Dict]) -> Optional[Dict]:
        """Handle plan review flow: send plan to assistant, get review, dispatch execution.

        Returns the final execution result, or None on failure.
        """
        original_task_id = plan_result.task_id
        plan = plan_result.result.get("plan", [])
        goal = plan_result.result.get("question", "")

        if not plan:
            logger.warning(f"plan_ready for {original_task_id} but no plan data")
            return plan_result.result

        original_task = self.task_repo.find_by_task_id(original_task_id)
        original_ctx = {}
        if original_task and original_task.context:
            try:
                original_ctx = json.loads(original_task.context)
            except (json.JSONDecodeError, TypeError):
                pass

        remaining = deadline - time.time()
        if remaining <= 0:
            logger.warning("Timeout before plan review could complete")
            return {"success": False, "error": "Plan review timeout"}

        assistant_name = assistant_role_info.get("name", "")
        assistant_sys_prompt = assistant_role_info.get("role", "")
        assistant_server_id = assistant_role_info.get("server_id")

        logger.info(f"Dispatching plan review for task {original_task_id} "
                    f"to assistant '{assistant_name}'")

        review_goal = (
            f"请审核以下执行计划并提出反馈。\n\n"
            f"【原始任务目标】\n{goal}\n\n"
            f"【执行计划】\n{json.dumps(plan, ensure_ascii=False, indent=2)}\n\n"
            f"【审核要求】\n"
            f"1. 检查计划是否覆盖了任务目标的所有方面\n"
            f"2. 检查每个步骤是否具体、可执行\n"
            f"3. 检查步骤顺序是否合理\n"
            f"4. 如果有改进建议，请明确指出需要修改的步骤和修改内容\n"
            f"5. 如果计划合理，回复'计划审核通过'\n\n"
            f"【回复格式】\n"
            f"- 如果通过：'计划审核通过'\n"
            f"- 如果需要修改：列出具体修改建议"
        )

        review_ctx = {
            "role": assistant_name,
            "system_prompt": assistant_sys_prompt,
            "task_type": "direct_execute",
            "question": review_goal,
        }
        if assistant_server_id:
            review_ctx["server_id"] = assistant_server_id

        review_task_id = str(uuid.uuid4())
        review_task = Task(
            task_id=review_task_id,
            parent_task_id=original_task_id,
            topic_id=None,
            scenario_id=scenario_id,
            idempotency_key=f"{original_task_id}:plan_review",
            depends_on=None,
            goal=review_goal,
            state=TaskState.PENDING.value,
            priority=0,
            timeout_seconds=min(int(remaining), 300),
            max_retries=1,
            retry_count=0,
            tenant_id=self.tenant_id,
            context=json.dumps(review_ctx, ensure_ascii=False),
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        self.task_repo.create(review_task)

        review_msg = TaskMessage(
            task_id=review_task_id,
            parent_task_id=original_task_id,
            goal=review_goal,
            context=review_ctx,
        )
        mqs.dispatch_subtasks(scenario_id, [review_msg], max_workers=1)

        review_timeout = max(int(min(remaining, 300)), 30)
        review_replies = mqs.collect_replies(
            scenario_id, 1, timeout=review_timeout,
            expected_task_ids=[review_task_id])

        if not review_replies:
            logger.warning(f"Plan review timeout for task {original_task_id}")
            return {"success": False, "error": "Plan review timeout"}

        review_output = review_replies[0].result.get("output", "")
        logger.info(f"Plan review result for {original_task_id}: "
                    f"{review_output[:200]}...")

        approved = self._is_plan_approved(review_output)

        exec_task_id = str(uuid.uuid4())
        if approved:
            exec_ctx = dict(original_ctx)
            exec_ctx["task_type"] = "execute_plan"
            exec_ctx["plan"] = plan
            exec_ctx.pop("review_feedback", None)
            exec_goal = goal
        else:
            exec_ctx = dict(original_ctx)
            exec_ctx["task_type"] = "revise_plan"
            exec_ctx["plan"] = plan
            exec_ctx["review_feedback"] = review_output
            exec_goal = f"{goal} (根据审核反馈修正)"

        exec_task = Task(
            task_id=exec_task_id,
            parent_task_id=original_task_id,
            topic_id=None,
            scenario_id=scenario_id,
            idempotency_key=f"{original_task_id}:plan_execution",
            depends_on=None,
            goal=exec_goal,
            state=TaskState.PENDING.value,
            priority=0,
            timeout_seconds=max(int(deadline - time.time()), 60),
            max_retries=1,
            retry_count=0,
            tenant_id=self.tenant_id,
            context=json.dumps(exec_ctx, ensure_ascii=False),
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        self.task_repo.create(exec_task)

        exec_msg = TaskMessage(
            task_id=exec_task_id,
            parent_task_id=original_task_id,
            goal=exec_goal,
            context=exec_ctx,
        )
        mqs.dispatch_subtasks(scenario_id, [exec_msg], max_workers=1)

        exec_timeout = max(int(deadline - time.time()), 60)
        exec_replies = mqs.collect_replies(
            scenario_id, 1, timeout=exec_timeout,
            expected_task_ids=[exec_task_id])

        if not exec_replies:
            logger.warning(f"Plan execution timeout for task {exec_task_id} "
                          f"(original: {original_task_id})")
            return {"success": False, "error": "Plan execution timeout after review"}

        return exec_replies[0].result

    def _heuristic_wave_check(
        self, wave_idx: int, waves: List[List[Dict]],
        wave_dispatched: List[str], replies: list,
        all_replies: Dict[str, dict], failed_ids: set,
        local_id_to_task_id: Dict[str, str],
        created_by_id: Dict[str, tuple],
        context: Dict[str, Any], configured_roles: List[Dict],
        assistant_roles: set,
    ) -> Optional[List[List[Dict]]]:
        """Evaluate wave results and regenerate remaining DAG if needed.

        Called after each wave completes (post plan_review). If any task's
        output is judged invalid (e.g. code-only text without actual execution),
        the remaining waves are replaced with LLM-regenerated tasks.

        Returns new waves list if regeneration happened, else None.
        """
        if not llm_client.client:
            return None

        original_goal = context.get("goal", "")

        completed_results = []
        failed_info = []
        wave_had_invalid = False

        for r in replies:
            tid = r.task_id
            sub_info = None
            for lid, (real_tid, sub) in created_by_id.items():
                if real_tid == tid:
                    sub_info = sub
                    break

            role = ""
            if sub_info:
                role = (sub_info.get("context") or {}).get("role", "")

            if role in assistant_roles:
                continue

            output = r.result.get("output", "") if isinstance(r.result, dict) else ""
            goal = (sub_info or {}).get("goal", "") if sub_info else ""

            if r.success and output:
                evaluation = llm_client.evaluate_task_output(goal, output, role)
                if not evaluation.get("valid", True):
                    logger.warning(
                        f"Heuristic check: task {tid} output invalid — "
                        f"{evaluation.get('reason', '')}")
                    wave_had_invalid = True
                    failed_ids.add(tid)
                    failed_info.append({
                        "goal": goal,
                        "output": output[:500],
                        "reason": evaluation.get("reason", ""),
                    })
                else:
                    completed_results.append({
                        "goal": goal, "output": output[:500],
                        "success": True, "role": role,
                    })
            elif not r.success:
                failed_info.append({
                    "goal": goal,
                    "output": (r.result.get("output", "") if isinstance(r.result, dict) else "")[:500],
                    "reason": r.result.get("error", "unknown") if isinstance(r.result, dict) else "failed",
                })

        if not wave_had_invalid:
            return None

        remaining_waves = waves[wave_idx + 1:]
        remaining_goals = []
        old_remaining_task_ids = []
        for w in remaining_waves:
            for sub in w:
                remaining_goals.append(sub.get("goal", ""))
                real_tid = local_id_to_task_id.get(sub["id"])
                if real_tid:
                    old_remaining_task_ids.append(real_tid)

        for old_tid in old_remaining_task_ids:
            try:
                self.task_repo.mark_as_cancelled(old_tid, "Superseded by heuristic regeneration")
            except Exception as e:
                logger.warning(f"Failed to cancel superseded task {old_tid}: {e}")

        logger.info(
            f"Heuristic check triggered after wave {wave_idx}: "
            f"{len(failed_info)} invalid task(s), "
            f"{len(remaining_goals)} remaining goal(s) to replan")

        exec_only_roles = [
            r for r in configured_roles if r.get("name") not in assistant_roles
        ]
        new_subtasks = llm_client.regenerate_remaining_tasks(
            original_goal=original_goal,
            completed_results=completed_results,
            remaining_goals=remaining_goals,
            failed_info=failed_info,
            configured_roles=exec_only_roles or configured_roles,
            context=context,
        )

        if not new_subtasks:
            logger.warning("Heuristic check: LLM regeneration returned empty, "
                           "keeping original DAG")
            return None

        new_id_map = {}
        for sub in new_subtasks:
            lid = sub["id"]
            real_tid = str(uuid.uuid4())
            new_id_map[lid] = real_tid
            sub["_real_task_id"] = real_tid

        for sub in new_subtasks:
            sub["depends_on"] = [
                new_id_map.get(d, d) for d in sub.get("depends_on", [])
            ]

        try:
            new_waves = self._build_waves(new_subtasks)
        except ValueError as e:
            logger.error(f"Heuristic check: failed to build waves from "
                         f"regenerated tasks: {e}")
            return None

        for sub in new_subtasks:
            real_tid = sub.pop("_real_task_id")
            lid = sub["id"]
            local_id_to_task_id[lid] = real_tid
            created_by_id[lid] = (real_tid, sub)

        logger.info(f"Heuristic check: replaced {len(remaining_goals)} remaining "
                     f"goals with {len(new_subtasks)} new tasks in "
                     f"{len(new_waves)} wave(s)")
        return new_waves

    @staticmethod
    def _is_plan_approved(review_output: str) -> bool:
        """Determine if the plan review indicates approval."""
        approval_keywords = ["审核通过", "计划通过", "通过", "approve", "approved",
                            "looks good", "no changes", "没有问题", "可以执行"]
        output_lower = review_output.lower()
        return any(kw in output_lower for kw in approval_keywords)
