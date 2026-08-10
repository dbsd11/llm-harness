# Scenario Manager - scenario lifecycle management
import uuid
import json
from typing import Dict, Any
from datetime import datetime

from database.repositories.scenario_repository import ScenarioRepository
from database.models.local_scenario import Scenario
from core.local_state_machine import ScenarioState
from core.local_event_bus import event_bus
from logger import logger


class ScenarioManager:
    """
    Scenario Manager: Lifecycle management and orchestration.

    Responsibilities:
    - Scenario registration and management
    - Scenario lifecycle (create/update)
    - Orphan recovery on startup

    Note: Scenario execution (start/stop/review) runs in websocket_server.
    ASP only manages the local DB records and event sync.
    """

    def __init__(self):
        self.scenario_repo = ScenarioRepository()

    def create_scenario(self, scenario_type: str, name: str, description: str = "",
                       config: Dict[str, Any] = None, created_by: int = None) -> str:
        """
        Create a new scenario.

        Args:
            scenario_type: Scenario type (simple_qa, code_execution)
            name: Scenario name
            description: Scenario description
            config: Scenario configuration
            created_by: User ID who created the scenario

        Returns:
            Scenario ID
        """
        scenario_id = str(uuid.uuid4())
        trace_id = str(uuid.uuid4())  # Trace for this scenario's lifecycle

        scenario = Scenario(
            scenario_id=scenario_id,
            scenario_type=scenario_type,
            name=name,
            description=description,
            state=ScenarioState.INITIALIZING.value,
            config=json.dumps(config or {}),
            context=json.dumps({"trace_id": trace_id}),
            created_by=created_by,
            created_at=datetime.now(),
            updated_at=datetime.now()
        )

        self.scenario_repo.create(scenario)

        event_bus.emit("scenario.created", {
            "scenario_id": scenario_id,
            "scenario_type": scenario_type,
            "name": name,
        }, trace_id=trace_id)

        logger.info(f"Created scenario: {scenario_id} ({scenario_type}) trace:{trace_id}")
        return scenario_id

    def update_scenario(self, scenario_id: str, name: str = None,
                        description: str = None, config: Dict[str, Any] = None,
                        scenario_type: str = None) -> bool:
        """更新已有场景的可编辑字段（scenario_type/name/description/config），不改状态。

        用于「确认保存」对已落库场景的二次修改：避免每次都新建一条记录。
        scenario_type 一并更新，否则修改类型（如 legacy debate → simple_qa）时类型字段
        不变，造成「保存了但查看没变化」+ 类型/配置不一致无法启动。
        """
        scenario = self.scenario_repo.find_by_scenario_id(scenario_id)
        if not scenario:
            logger.error(f"update_scenario: scenario not found: {scenario_id}")
            return False

        # Enforce editability rule (D6): manual_acceptance editable only while initializing
        if config is not None and scenario.state != ScenarioState.INITIALIZING.value:
            if "manual_acceptance" in config:
                logger.warning(f"update_scenario: manual_acceptance cannot be changed "
                               f"after scenario leaves initializing state")
                # Strip manual_acceptance from config, proceed with other fields
                config = {k: v for k, v in config.items() if k != "manual_acceptance"}

        if scenario_type is not None:
            scenario.scenario_type = scenario_type
        if name is not None:
            scenario.name = name
        if description is not None:
            scenario.description = description
        if config is not None:
            scenario.config = json.dumps(config)
        scenario.updated_at = datetime.now()

        ok = self.scenario_repo.update(scenario)

        # 传播 trace_id 用于事件追踪
        trace_id = ""
        try:
            trace_id = json.loads(scenario.context or "{}").get("trace_id", "") or ""
        except (json.JSONDecodeError, TypeError):
            pass
        event_bus.emit("scenario.updated", {
            "scenario_id": scenario_id,
            "name": scenario.name,
        }, trace_id=trace_id)

        logger.info(f"Updated scenario: {scenario_id}")
        return ok


# Global scenario manager instance
scenario_manager = ScenarioManager()


def recover_orphans_on_startup() -> None:
    """Mark in-flight scenarios/tasks orphaned by a platform restart as failed.

    Scenario execution threads live in the Flask process; task execution lives
    in the WS process. A platform restart kills both, so any scenario still
    `running` and any task still `pending`/`running`/`waiting` has no live
    runner and would otherwise stay "running" forever. This makes them visible
    failures instead. Full scenario resume (checkpointing) is future work.

    Idempotent: only touches non-terminal rows, so repeated restarts are safe.
    """
    from database.repositories.task_repository import TaskRepository

    srepo = ScenarioRepository()
    trepo = TaskRepository()

    n_scenarios = 0
    for s in srepo.find_by_state(ScenarioState.RUNNING.value):
        srepo.mark_as_failed(s.scenario_id)
        n_scenarios += 1

    n_tasks = 0
    for state in ("pending", "running", "waiting"):
        for t in trepo.find_by_state(state):
            trepo.mark_as_failed(t.task_id, "Orphaned by platform restart")
            n_tasks += 1

    if n_scenarios or n_tasks:
        logger.info(f"Startup orphan recovery: marked {n_scenarios} scenario(s) "
                    f"and {n_tasks} task(s) as failed")
        event_bus.emit("recovery.orphans_marked", {
            "scenarios": n_scenarios, "tasks": n_tasks,
        })
    else:
        logger.info("Startup orphan recovery: no orphans found")
