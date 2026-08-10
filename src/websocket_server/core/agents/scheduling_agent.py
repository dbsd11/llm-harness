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
from core.state_machine import TaskState, TASK_STATE_MACHINE
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
    """

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
                logger.info(f"Starting goal decomposition for task {task_id}")
                subtasks = self._decompose_goal(goal, context)
                subtasks = self._normalize_subtasks(subtasks)
                logger.info(f"Goal decomposition completed: {len(subtasks)} subtasks created")

                local_id_to_task_id: Dict[str, str] = {}
                created_by_id: Dict[str, tuple] = {}
                for sub in subtasks:
                    lid = sub["id"]
                    deps = [d for d in (sub.get("depends_on") or [])]
                    subtask_id = self._create_subtask(
                        task_id, sub, topic_id=topic_id, scenario_id=scenario_id,
                        depends_on=deps, local_id_to_task_id=local_id_to_task_id,
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
                total_timeout = context.get("timeout_seconds", 300)
                deadline = time.time() + total_timeout
                all_replies: Dict[str, dict] = {}
                failed_ids: set = set()
                gated = False

                for wave_idx, wave in enumerate(waves):
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        logger.warning(f"SchedulingAgent: timeout before wave {wave_idx}")
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
                                "task_id": tid, "reason": "predecessor failed"})
                            continue

                        ctx = dict(sub.get("context", {}) or {})
                        role, sys_prompt, server_id = self._associate_role(
                            sub, configured_roles)
                        ctx["role"] = role
                        ctx["system_prompt"] = sys_prompt
                        if server_id:
                            ctx["server_id"] = server_id
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
                    mqs.dispatch_subtasks(scenario_id, wave_msgs, max_workers=max_workers)

                    wave_timeout = max(int(remaining), 1)
                    replies = mqs.collect_replies(
                        scenario_id, len(wave_msgs), timeout=wave_timeout)
                    for r in replies:
                        all_replies[r.task_id] = r.result
                        if r.pending_review:
                            gated = True
                        elif not r.success:
                            failed_ids.add(r.task_id)

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

                    # Propagate failure to all remaining dependents.
                    if failed_ids:
                        self._propagate_failure(
                            failed_ids, waves, local_id_to_task_id, wave_idx + 1)

                event_bus.emit("task.scheduled", {
                    "task_id": task_id,
                    "topic_id": topic_id,
                    "subtask_count": len(subtask_ids),
                    "subtask_ids": subtask_ids,
                })

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
            })

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
            })

            return {
                "success": False,
                "error": error_msg,
            }

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
                        })
                        skipped.add(tid)
        return skipped

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
            servers = ExecutionServerRepository().list_all()
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
        timeout = context.get("timeout_seconds", 3600)
        exec_context = context.get("execution_context", {})

        logger.info(f"Attempting goal decomposition for: {goal[:100]}...")

        # Pass configured execution-agent role names to the LLM so it picks
        # from them (rather than inventing names that won't route).
        configured_roles = (context.get("agent_roles") or {}).get(
            "execution_agents", []) or []
        if configured_roles:
            context = dict(context)
            context["execution_role_names"] = [r.get("name", "") for r in configured_roles]

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
                        local_id_to_task_id: Dict[str, str] = None) -> Optional[str]:
        """
        Create a subtask with idempotency control (幂等控制).

        If a task with the same idempotency_key already exists, returns
        the existing task_id instead of creating a duplicate.

        Args:
            depends_on: local predecessor ids (e.g. ["t1"])
            local_id_to_task_id: map to resolve local ids -> real task_ids
                for persistence. The new task's own id is registered here.

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
            timeout_seconds=subtask.get("timeout_seconds", 3600),
            max_retries=3,
            retry_count=0,
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
        })

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
            timeout_seconds=context.get("timeout_seconds", 3600) if context else 3600,
            max_retries=3,
            retry_count=0,
            context=json.dumps(context, ensure_ascii=False) if context else "{}",
            created_at=datetime.now(),
            updated_at=datetime.now()
        )

        self.task_repo.create(task)

        event_bus.emit("task.created", {
            "task_id": task_id,
            "goal": goal,
        })

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
        })

        logger.info(f"Task {task_id} state: {old_state} -> {new_state.value}")
        return True

    def cleanup(self) -> None:
        """Cleanup resources"""
        logger.info("SchedulingAgent cleaned up")
