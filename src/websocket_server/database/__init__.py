# Database initialization
import logging
from typing import List, Type

from .connection import get_connection_manager
from .models.base import BaseModel
from .models.user import User
from .models.agent import Agent
from .models.task import Task
from .models.scenario import Scenario
from .models.event import Event
from .models.message import Message
from .models.consumer_offset import ConsumerOffset
from .models.execution_server import ExecutionServer
from .models.assistant_message import AssistantMessage
from .models.human_task import HumanTask

from .repositories.base_repository import BaseRepository

from logger import logger


def init_database():
    """Initialize database - create all tables and run migrations"""
    try:
        # Create all tables
        models: List[Type[BaseModel]] = [
            User,
            Agent,
            Task,
            Scenario,
            Event,
            Message,
            ConsumerOffset,
            ExecutionServer,
            AssistantMessage,
            HumanTask,
        ]

        for model_class in models:
            repository = BaseRepository(model_class)
            repository.create_table_if_not_exists()
            logger.info(f"Created table: {model_class.__tablename__}")

        # Run migrations (add columns that may be missing from older DBs)
        from .repositories.task_repository import TaskRepository
        task_repo = TaskRepository()
        task_repo._ensure_columns()
        logger.info("Task table migration complete")

        from .repositories.event_repository import EventRepository
        event_repo = EventRepository()
        event_repo._ensure_columns()
        logger.info("Event table migration complete")

        from .repositories.execution_server_repository import ExecutionServerRepository
        ExecutionServerRepository()._ensure_columns()
        logger.info("ExecutionServer table migration complete")

        from .repositories.assistant_message_repository import AssistantMessageRepository
        AssistantMessageRepository()._ensure_columns()
        logger.info("AssistantMessage table migration complete")

        # 创建关键查询索引，避免 dispatch 轮询 / 心跳清扫 / task 查询走全表扫描
        # （全表扫描是 event loop 被同步 DB I/O 阻塞的主要放大因素之一）。
        _indexes = [
            "CREATE INDEX IF NOT EXISTS idx_messages_dispatch_acked ON messages(message_type, acked, id)",
            "CREATE INDEX IF NOT EXISTS idx_messages_task_id ON messages(task_id)",
            "CREATE INDEX IF NOT EXISTS idx_messages_scenario_id ON messages(scenario_id)",
            "CREATE INDEX IF NOT EXISTS idx_exec_servers_conn_hb ON execution_servers(connected, last_heartbeat)",
            "CREATE INDEX IF NOT EXISTS idx_tasks_scenario_id ON tasks(scenario_id)",
            "CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)",
        ]
        try:
            cm = get_connection_manager()
            with cm.get_connection() as conn:
                cursor = conn.cursor()
                for stmt in _indexes:
                    try:
                        cursor.execute(stmt)
                    except Exception as ie:
                        logger.warning(f"创建索引失败（可忽略）: {ie}")
                conn.commit()
            logger.info("Database indexes ensured")
        except Exception as e:
            logger.warning(f"创建索引时出错（可忽略）: {e}")

        return True
    except Exception as e:
        logger.error(f"Failed to initialize database: {str(e)}")
        return False


def close_database():
    """Close database connections"""
    try:
        connection_manager = get_connection_manager()
        connection_manager.close_all()
        logger.info("Closed all database connections")
        return True
    except Exception as e:
        logger.error(f"Failed to close database: {str(e)}")
        return False
