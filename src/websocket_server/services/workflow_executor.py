"""Executes a published workflow DAG.

Materializes workflow templates into concrete tasks, dispatches them via the
existing message queue / CentralDispatcher pipeline, and collects results
wave-by-wave in topological order. No LLM decomposition -- goals are pre-rendered
from templates with parameter substitution, and experience context from prior
runs is injected so execution agents have historical guidance.
"""
import json
import time
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from string import Template
from typing import Any, Dict, List, Optional, Tuple

from database.models.scenario import Scenario
from database.models.task import Task
from database.models.workflow import Workflow
from database.models.workflow_execution import WorkflowExecution
from database.models.workflow_task_template import WorkflowTaskTemplate
from database.repositories.scenario_repository import ScenarioRepository
from database.repositories.task_repository import TaskRepository
from database.repositories.execution_server_repository import ExecutionServerRepository
from database.repositories.workflow_repository import WorkflowRepository
from database.repositories.workflow_execution_repository import WorkflowExecutionRepository
from database.repositories.workflow_task_template_repository import WorkflowTaskTemplateRepository
from core.message_queue import TaskMessage, mqs
from core.event_bus import event_bus
from logger import logger


class WorkflowExecutor:

    def __init__(self):
        self.workflow_repo = WorkflowRepository()
        self.execution_repo = WorkflowExecutionRepository()
        self.template_repo = WorkflowTaskTemplateRepository()
        self.task_repo = TaskRepository()
        self.scenario_repo = ScenarioRepository()
        self.server_repo = ExecutionServerRepository()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wf-exec")

    def execute(self, workflow_id: str, input_params: Dict[str, Any],
                created_by: str = None) -> Dict[str, Any]:
        workflow = self.workflow_repo.find_by_workflow_id(workflow_id)
        if not workflow:
            raise ValueError(f"Workflow {workflow_id} not found")
        if workflow.state != "active":
            raise ValueError(f"Workflow is not active (state: {workflow.state})")

        ok, err = self._validate_inputs(workflow, input_params)
        if not ok:
            raise ValueError(f"Input validation failed: {err}")

        templates = self.template_repo.find_by_workflow_id(workflow_id)
        if not templates:
            raise ValueError("No task templates found for workflow")

        offline_servers = self._check_servers(templates)
        if offline_servers:
            logger.warning(f"Workflow {workflow_id} references offline servers: "
                           f"{', '.join(offline_servers)} — tasks will be deferred "
                           f"until servers reconnect")

        execution_id = str(uuid.uuid4())
        now = datetime.now()
        execution = WorkflowExecution(
            execution_id=execution_id,
            workflow_id=workflow_id,
            workflow_version=workflow.version,
            state="validating",
            input_params=json.dumps(input_params, ensure_ascii=False),
            task_results="{}",
            total_steps=len(templates),
            completed_steps=0,
            failed_steps=0,
            error=None,
            created_at=now,
            updated_at=now,
        )
        self.execution_repo.create(execution)

        self._executor.submit(self._run_execution, execution_id, workflow, templates, input_params)

        event_bus.emit("workflow.execution_started", {
            "execution_id": execution_id,
            "workflow_id": workflow_id,
            "step_count": len(templates),
        })

        return {
            "execution_id": execution_id,
            "workflow_id": workflow_id,
            "state": "validating",
            "total_steps": len(templates),
        }

    def _run_execution(self, execution_id: str, workflow: Workflow,
                       templates: List[WorkflowTaskTemplate],
                       input_params: Dict[str, Any]) -> None:
        parent_task_id = None
        scenario_id = None
        try:
            self.execution_repo.mark_as_running(execution_id)

            scenario_id = self._create_execution_scenario(execution_id, workflow)

            step_id_to_task_id, parent_task_id = self._materialize_tasks(
                scenario_id, execution_id, templates, input_params)

            self.execution_repo.update_scenario_link(
                execution_id, scenario_id, parent_task_id)

            waves = self._build_waves(templates)
            replies_by_task_id: Dict[str, dict] = {}
            task_results: Dict[str, dict] = {}
            completed_count = 0
            failed_count = 0

            for wave_idx, wave_templates in enumerate(waves):
                messages: List[TaskMessage] = []
                for tmpl in wave_templates:
                    task_id = step_id_to_task_id[tmpl.step_id]
                    goal = self._render_goal(tmpl.goal_template, input_params)

                    ctx = {
                        "role": tmpl.agent_role or "通用助手",
                        "system_prompt": tmpl.system_prompt or "你是一个有帮助的智能助手。",
                        "question": goal,
                        "is_workflow_task": True,
                        "workflow_execution_id": execution_id,
                    }

                    if tmpl.server_id:
                        ctx["server_id"] = tmpl.server_id

                    exp_ctx = self._build_experience_context(tmpl)
                    if exp_ctx:
                        ctx["experience_context"] = exp_ctx

                    dep_task_ids = self._resolve_dep_task_ids(tmpl, step_id_to_task_id)
                    if dep_task_ids:
                        self._inject_upstream(ctx, dep_task_ids, replies_by_task_id)

                    messages.append(TaskMessage(
                        task_id=task_id,
                        parent_task_id=parent_task_id,
                        goal=goal,
                        context=ctx,
                    ))

                mqs.dispatch_subtasks(scenario_id, messages)

                timeout = max((t.timeout_seconds or 300) for t in wave_templates)
                replies = mqs.collect_replies(scenario_id, len(messages), timeout=timeout)

                for reply in replies:
                    replies_by_task_id[reply.task_id] = reply.result
                    step_id = self._task_id_to_step_id(reply.task_id, step_id_to_task_id)
                    task_results[step_id] = {
                        "state": "success" if reply.success else "failed",
                        "result": reply.result,
                    }
                    if reply.success:
                        completed_count += 1
                    else:
                        failed_count += 1

                failed_in_wave = [r.task_id for r in replies if not r.success]
                if failed_in_wave and wave_idx < len(waves) - 1:
                    self._propagate_failure(
                        failed_in_wave, waves, step_id_to_task_id,
                        wave_idx + 1, scenario_id)

                self.execution_repo.update_progress(
                    execution_id, completed_count, failed_count,
                    json.dumps(task_results, ensure_ascii=False))

            if failed_count > 0:
                self.execution_repo.mark_as_failed(
                    execution_id, f"{failed_count} step(s) failed")
                if parent_task_id:
                    self.task_repo.mark_as_failed(
                        parent_task_id, f"{failed_count} step(s) failed")
                self.scenario_repo.update_scenario_state(scenario_id, "failed")
                event_bus.emit("workflow.execution_failed", {
                    "execution_id": execution_id,
                    "workflow_id": workflow.workflow_id,
                    "failed_steps": failed_count,
                })
            else:
                self.execution_repo.mark_as_completed(execution_id)
                if parent_task_id:
                    self.task_repo.mark_as_completed(parent_task_id)
                self.scenario_repo.update_scenario_state(scenario_id, "completed")
                event_bus.emit("workflow.execution_completed", {
                    "execution_id": execution_id,
                    "workflow_id": workflow.workflow_id,
                    "completed_steps": completed_count,
                })

            logger.info(f"Workflow execution {execution_id} finished: "
                        f"{completed_count} completed, {failed_count} failed")

        except Exception as e:
            logger.error(f"Workflow execution {execution_id} error: {e}")
            self.execution_repo.mark_as_failed(execution_id, str(e))
            if parent_task_id:
                self.task_repo.mark_as_failed(parent_task_id, str(e))
            if scenario_id:
                self.scenario_repo.update_scenario_state(scenario_id, "failed")
            event_bus.emit("workflow.execution_failed", {
                "execution_id": execution_id,
                "workflow_id": workflow.workflow_id,
                "error": str(e),
            })

    def _validate_inputs(self, workflow: Workflow, params: Dict) -> Tuple[bool, str]:
        try:
            schema = json.loads(workflow.input_schema) if workflow.input_schema else {}
        except (json.JSONDecodeError, TypeError):
            return True, ""

        required = schema.get("required", [])
        for field in required:
            if field not in params:
                return False, f"Missing required parameter: {field}"

        properties = schema.get("properties", {})
        for key, val in params.items():
            if key in properties:
                expected_type = properties[key].get("type", "string")
                if expected_type == "string" and not isinstance(val, str):
                    return False, f"Parameter '{key}' must be a string"
                elif expected_type == "number" and not isinstance(val, (int, float)):
                    return False, f"Parameter '{key}' must be a number"
                elif expected_type == "integer" and not isinstance(val, int):
                    return False, f"Parameter '{key}' must be an integer"
                elif expected_type == "boolean" and not isinstance(val, bool):
                    return False, f"Parameter '{key}' must be a boolean"

        return True, ""

    def _check_servers(self, templates: List[WorkflowTaskTemplate]) -> List[str]:
        offline = []
        checked = set()
        for tmpl in templates:
            if tmpl.server_id and tmpl.server_id not in checked:
                checked.add(tmpl.server_id)
                server = self.server_repo.find_by_server_id(tmpl.server_id)
                if not server or not server.connected:
                    offline.append(tmpl.server_id)
        return offline

    def _create_execution_scenario(self, execution_id: str, workflow: Workflow) -> str:
        scenario_id = str(uuid.uuid4())
        now = datetime.now()
        scenario = Scenario(
            scenario_id=scenario_id,
            scenario_type="workflow_execution",
            name=f"Workflow Execution: {workflow.name} ({execution_id[:8]})",
            description=f"Auto-created for workflow execution {execution_id}",
            state="running",
            config=json.dumps({
                "workflow_id": workflow.workflow_id,
                "execution_id": execution_id,
                "is_workflow_execution": True,
            }, ensure_ascii=False),
            context=json.dumps({"trace_id": execution_id}, ensure_ascii=False),
            created_by=None,
            created_at=now,
            updated_at=now,
            started_at=now,
        )
        self.scenario_repo.create(scenario)
        return scenario_id

    def _materialize_tasks(self, scenario_id: str, execution_id: str,
                           templates: List[WorkflowTaskTemplate],
                           input_params: Dict[str, Any]) -> Tuple[Dict[str, str], str]:
        parent_task_id = str(uuid.uuid4())
        now = datetime.now()
        parent_task = Task(
            task_id=parent_task_id,
            scenario_id=scenario_id,
            goal=f"Workflow execution: {execution_id}",
            state="running",
            priority=0,
            timeout_seconds=3600,
            context=json.dumps({
                "workflow_id": templates[0].workflow_id if templates else "",
                "execution_id": execution_id,
                "is_workflow_task": True,
            }, ensure_ascii=False),
            created_at=now,
            updated_at=now,
        )
        self.task_repo.create(parent_task)

        step_id_to_task_id: Dict[str, str] = {}
        for tmpl in templates:
            task_id = str(uuid.uuid4())
            step_id_to_task_id[tmpl.step_id] = task_id

        for tmpl in templates:
            task_id = step_id_to_task_id[tmpl.step_id]
            goal = self._render_goal(tmpl.goal_template, input_params)

            dep_task_ids = self._resolve_dep_task_ids(tmpl, step_id_to_task_id)
            exp_ctx = self._build_experience_context(tmpl)

            ctx = {
                "role": tmpl.agent_role or "通用助手",
                "system_prompt": tmpl.system_prompt or "你是一个有帮助的智能助手。",
                "question": goal,
                "is_workflow_task": True,
                "workflow_execution_id": execution_id,
                "experience_context": exp_ctx,
            }
            if tmpl.server_id:
                ctx["server_id"] = tmpl.server_id

            task = Task(
                task_id=task_id,
                scenario_id=scenario_id,
                parent_task_id=parent_task_id,
                depends_on=json.dumps(dep_task_ids, ensure_ascii=False),
                goal=goal,
                state="pending",
                priority=0,
                timeout_seconds=tmpl.timeout_seconds or 300,
                context=json.dumps(ctx, ensure_ascii=False),
                agent_role=tmpl.agent_role,
                created_at=now,
                updated_at=now,
            )
            self.task_repo.create(task)

        return step_id_to_task_id, parent_task_id

    def _render_goal(self, template_str: str, params: Dict[str, Any]) -> str:
        if "{{" not in template_str:
            return template_str
        result = template_str
        for key, val in params.items():
            result = result.replace("{{" + key + "}}", str(val))
        return result

    def _build_experience_context(self, tmpl: WorkflowTaskTemplate) -> Optional[Dict]:
        if not tmpl.experience_note:
            return None
        try:
            note = json.loads(tmpl.experience_note)
            return {
                "previous_state": note.get("previous_state"),
                "previous_result_summary": note.get("previous_result_summary", ""),
                "previous_error": note.get("previous_error"),
                "tips": note.get("tips", ""),
            }
        except (json.JSONDecodeError, TypeError):
            return None

    def _resolve_dep_task_ids(self, tmpl: WorkflowTaskTemplate,
                              step_id_to_task_id: Dict[str, str]) -> List[str]:
        if not tmpl.depends_on:
            return []
        try:
            dep_step_ids = json.loads(tmpl.depends_on)
        except (json.JSONDecodeError, TypeError):
            return []
        return [step_id_to_task_id[sid] for sid in dep_step_ids if sid in step_id_to_task_id]

    def _task_id_to_step_id(self, task_id: str,
                            step_id_to_task_id: Dict[str, str]) -> str:
        for sid, tid in step_id_to_task_id.items():
            if tid == task_id:
                return sid
        return task_id

    def _build_waves(self, templates: List[WorkflowTaskTemplate]) -> List[List[WorkflowTaskTemplate]]:
        by_step = {t.step_id: t for t in templates}
        deps: Dict[str, List[str]] = {}
        for t in templates:
            dep_ids = []
            if t.depends_on:
                try:
                    raw = json.loads(t.depends_on)
                    dep_ids = [d for d in raw if d in by_step and d != t.step_id]
                except (json.JSONDecodeError, TypeError):
                    pass
            deps[t.step_id] = dep_ids

        waves: List[List[WorkflowTaskTemplate]] = []
        placed: set = set()
        remaining = set(by_step.keys())

        while remaining:
            ready = [sid for sid in remaining if all(d in placed for d in deps[sid])]
            if not ready:
                raise ValueError(f"Dependency cycle detected among: {sorted(remaining)}")
            waves.append([by_step[sid] for sid in ready])
            placed.update(ready)
            remaining -= set(ready)

        return waves

    def _inject_upstream(self, ctx: Dict, dep_task_ids: List[str],
                         replies_by_task_id: Dict[str, dict]) -> None:
        if not dep_task_ids:
            return
        upstream = {}
        outputs = []
        for tid in dep_task_ids:
            if tid not in replies_by_task_id:
                continue
            res = replies_by_task_id[tid]
            upstream[tid] = res
            outputs.append(res.get("output", ""))
        ctx["upstream_results"] = upstream
        ctx["upstream_outputs"] = outputs

    def _propagate_failure(self, failed_task_ids: List[str],
                           waves: List[List[WorkflowTaskTemplate]],
                           step_id_to_task_id: Dict[str, str],
                           start_wave: int, scenario_id: str) -> None:
        failed_set = set(failed_task_ids)
        step_to_task = step_id_to_task_id

        for wave in waves[start_wave:]:
            for tmpl in wave:
                dep_task_ids = self._resolve_dep_task_ids(tmpl, step_to_task)
                if any(d in failed_set for d in dep_task_ids):
                    tid = step_to_task.get(tmpl.step_id)
                    if tid:
                        try:
                            self.task_repo.mark_as_failed(tid, "Skipped: predecessor step failed")
                        except Exception as e:
                            logger.error(f"Failed to mark skipped task {tid}: {e}")
                        event_bus.emit("task.skipped", {
                            "task_id": tid, "reason": "predecessor failed",
                        })


workflow_executor = WorkflowExecutor()
