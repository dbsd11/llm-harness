"""Publishes a completed scenario as a reusable Workflow DAG.

Extracts task structure, dependencies, agent roles, server assignments,
and success/failure experiences into a parameterized workflow definition
that can be executed repeatedly with different inputs -- no LLM parsing needed.
"""
import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from database.models.workflow import Workflow
from database.models.workflow_task_template import WorkflowTaskTemplate
from database.repositories.scenario_repository import ScenarioRepository
from database.repositories.task_repository import TaskRepository
from database.repositories.workflow_repository import WorkflowRepository
from database.repositories.workflow_task_template_repository import WorkflowTaskTemplateRepository
from core.event_bus import event_bus
from logger import logger

_TERMINAL_STATES = {"success", "failed"}


class WorkflowPublisher:

    def __init__(self):
        self.scenario_repo = ScenarioRepository()
        self.task_repo = TaskRepository()
        self.workflow_repo = WorkflowRepository()
        self.template_repo = WorkflowTaskTemplateRepository()

    def publish(self, scenario_id: str, name: str = None,
                description: str = None, created_by: str = None) -> Dict[str, Any]:
        scenario = self.scenario_repo.find_by_scenario_id(scenario_id)
        if not scenario:
            raise ValueError(f"Scenario {scenario_id} not found")
        if scenario.state not in ("completed", "failed"):
            raise ValueError(
                f"Can only publish completed/failed scenarios, current state: {scenario.state}")

        all_tasks = self.task_repo.find_by_scenario_id(scenario_id)
        tasks = [t for t in all_tasks if t.state in _TERMINAL_STATES]
        if not tasks:
            raise ValueError("No completed/failed tasks found in scenario")

        task_id_to_step_id, steps = self._build_dag(tasks)
        scenario_config = json.loads(scenario.config) if scenario.config else {}
        param_map, per_step_mapping = self._extract_parameters(scenario_config, tasks, task_id_to_step_id)
        experiences = self._collect_experiences(tasks, task_id_to_step_id)
        input_schema = self._generate_input_schema(param_map)

        dag_definition = json.dumps({"steps": steps}, ensure_ascii=False)
        agent_roles = self._extract_agent_roles(tasks)

        existing = self.workflow_repo.find_by_source_scenario_id(scenario_id)
        version = 1
        if existing:
            version = max(w.version or 1 for w in existing) + 1

        workflow_id = str(uuid.uuid4())
        wf_name = name or f"Workflow from: {scenario.name}"

        now = datetime.now()
        workflow = Workflow(
            workflow_id=workflow_id,
            name=wf_name,
            description=description or f"Published from scenario {scenario.name} ({scenario_id})",
            source_scenario_id=scenario_id,
            dag_definition=dag_definition,
            input_schema=json.dumps(input_schema, ensure_ascii=False),
            agent_roles=json.dumps(agent_roles, ensure_ascii=False),
            experience_context=json.dumps(experiences, ensure_ascii=False),
            version=version,
            state="active",
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        self.workflow_repo.create(workflow)

        self._persist_templates(workflow_id, steps, per_step_mapping, experiences, tasks, task_id_to_step_id)

        event_bus.emit("workflow.published", {
            "workflow_id": workflow_id,
            "name": wf_name,
            "source_scenario_id": scenario_id,
            "version": version,
            "step_count": len(steps),
        })

        logger.info(f"Published workflow {workflow_id} from scenario {scenario_id} "
                     f"with {len(steps)} steps, version {version}")

        return {
            "workflow_id": workflow_id,
            "name": wf_name,
            "version": version,
            "step_count": len(steps),
            "input_schema": input_schema,
            "dag_definition": json.loads(dag_definition),
            "experience_context": experiences,
        }

    def _build_dag(self, tasks: List) -> Tuple[Dict[str, str], List[Dict]]:
        task_id_to_step_id: Dict[str, str] = {}
        for idx, task in enumerate(tasks):
            task_id_to_step_id[task.task_id] = f"step_{idx + 1}"

        waves = self._topological_sort(tasks, task_id_to_step_id)
        step_order_map: Dict[str, int] = {}
        order = 0
        for wave in waves:
            for step in wave:
                order += 1
                step_order_map[step["step_id"]] = order

        steps = []
        for task in tasks:
            step_id = task_id_to_step_id[task.task_id]
            ctx = json.loads(task.context) if task.context else {}
            dep_step_ids = []
            if task.depends_on:
                try:
                    dep_task_ids = json.loads(task.depends_on)
                    dep_step_ids = [task_id_to_step_id[tid] for tid in dep_task_ids
                                    if tid in task_id_to_step_id]
                except (json.JSONDecodeError, TypeError):
                    pass

            steps.append({
                "step_id": step_id,
                "source_task_id": task.task_id,
                "goal_template": task.goal or "",
                "depends_on": dep_step_ids,
                "agent_role": task.agent_role or "",
                "system_prompt": ctx.get("system_prompt", ""),
                "server_id": ctx.get("server_id", ""),
                "timeout_seconds": task.timeout_seconds or 300,
                "step_order": step_order_map.get(step_id, 0),
            })

        steps.sort(key=lambda s: s["step_order"])
        return task_id_to_step_id, steps

    def _topological_sort(self, tasks: List, task_id_to_step_id: Dict[str, str]) -> List[List[Dict]]:
        by_id = {t.task_id: t for t in tasks}
        deps: Dict[str, List[str]] = {}
        for t in tasks:
            dep_ids = []
            if t.depends_on:
                try:
                    raw = json.loads(t.depends_on)
                    dep_ids = [d for d in raw if d in by_id and d != t.task_id]
                except (json.JSONDecodeError, TypeError):
                    pass
            deps[t.task_id] = dep_ids

        waves: List[List[Dict]] = []
        placed: set = set()
        remaining = set(by_id.keys())

        while remaining:
            ready = [tid for tid in remaining if all(d in placed for d in deps[tid])]
            if not ready:
                break
            wave = []
            for tid in ready:
                step_id = task_id_to_step_id[tid]
                wave.append({"step_id": step_id, "task_id": tid})
            waves.append(wave)
            placed.update(ready)
            remaining -= set(ready)

        return waves

    def _extract_parameters(self, scenario_config: Dict, tasks: List,
                            task_id_to_step_id: Dict[str, str]) -> Tuple[Dict, Dict[str, Dict]]:
        param_map: Dict[str, Any] = {}
        per_step_mapping: Dict[str, Dict[str, str]] = {}

        config_values: Dict[str, str] = {}
        for key, val in scenario_config.items():
            if isinstance(val, str) and len(val) >= 3:
                config_values[key] = val

        for task in tasks:
            if task.task_id not in task_id_to_step_id:
                continue
            step_id = task_id_to_step_id[task.task_id]
            goal = task.goal or ""
            step_mapping: Dict[str, str] = {}

            for param_name, param_val in config_values.items():
                if param_val in goal:
                    template_var = param_name
                    if template_var not in param_map:
                        param_map[template_var] = {
                            "type": "string",
                            "description": f"Parameter: {param_name}",
                            "example": param_val[:100],
                        }
                    step_mapping[template_var] = param_name

            if step_mapping:
                per_step_mapping[step_id] = step_mapping

        return param_map, per_step_mapping

    def _collect_experiences(self, tasks: List,
                             task_id_to_step_id: Dict[str, str]) -> List[Dict]:
        experiences = []
        for task in tasks:
            if task.task_id not in task_id_to_step_id:
                continue
            step_id = task_id_to_step_id[task.task_id]
            result_summary = ""
            if task.result:
                try:
                    result_data = json.loads(task.result)
                    result_summary = str(result_data.get("output", ""))[:500]
                except (json.JSONDecodeError, TypeError):
                    result_summary = str(task.result)[:500]

            experiences.append({
                "step_id": step_id,
                "task_id": task.task_id,
                "state": task.state,
                "goal": task.goal or "",
                "result_summary": result_summary,
                "error": task.error or None,
                "review_feedback": task.review_feedback or None,
                "execution_duration": task.execution_duration,
            })
        return experiences

    def _generate_input_schema(self, param_map: Dict) -> Dict:
        properties = {}
        required = []
        for name, info in param_map.items():
            properties[name] = {
                "type": info.get("type", "string"),
                "description": info.get("description", ""),
            }
            if info.get("example"):
                properties[name]["example"] = info["example"]
            required.append(name)

        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    def _extract_agent_roles(self, tasks: List) -> Dict:
        roles: Dict[str, Dict] = {}
        for task in tasks:
            role = task.agent_role
            if role and role not in roles:
                ctx = json.loads(task.context) if task.context else {}
                roles[role] = {
                    "role": role,
                    "system_prompt": ctx.get("system_prompt", ""),
                }
        return roles

    def _persist_templates(self, workflow_id: str, steps: List[Dict],
                           per_step_mapping: Dict[str, Dict[str, str]],
                           experiences: List[Dict], tasks: List,
                           task_id_to_step_id: Dict[str, str]) -> None:
        exp_by_step = {e["step_id"]: e for e in experiences}
        now = datetime.now()

        for step in steps:
            step_id = step["step_id"]
            goal_template = step["goal_template"]

            step_mapping = per_step_mapping.get(step_id, {})
            for template_var, param_name in step_mapping.items():
                config_val = ""
                for t in tasks:
                    if task_id_to_step_id.get(t.task_id) == step_id:
                        scenario_config_raw = t.context or "{}"
                        break
                goal_template = goal_template.replace(
                    step_mapping.get(template_var, template_var),
                    "{{" + template_var + "}}",
                    1,
                )

            exp = exp_by_step.get(step_id, {})
            experience_note = json.dumps({
                "previous_state": exp.get("state"),
                "previous_result_summary": exp.get("result_summary", "")[:200],
                "previous_error": exp.get("error"),
                "tips": f"Previously {exp.get('state', 'unknown')} in {exp.get('execution_duration', 'N/A')}s",
            }, ensure_ascii=False)

            template = WorkflowTaskTemplate(
                template_id=str(uuid.uuid4()),
                workflow_id=workflow_id,
                step_id=step_id,
                goal_template=goal_template,
                depends_on=json.dumps(step["depends_on"], ensure_ascii=False),
                agent_role=step["agent_role"],
                system_prompt=step.get("system_prompt", ""),
                server_id=step.get("server_id", ""),
                timeout_seconds=step.get("timeout_seconds", 300),
                input_param_mapping=json.dumps(step_mapping, ensure_ascii=False),
                experience_note=experience_note,
                step_order=step["step_order"],
                created_at=now,
                updated_at=now,
            )
            self.template_repo.create(template)


workflow_publisher = WorkflowPublisher()
