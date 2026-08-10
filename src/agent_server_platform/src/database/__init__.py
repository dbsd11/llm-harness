# Database initialization
import logging
from typing import List, Type

from .local_connection import get_connection_manager
from .models.local_base import BaseModel
from .models.local_user import User
from .models.local_agent import Agent
from .models.local_task import Task
from .models.local_scenario import Scenario
from .models.local_event import Event
from .models.local_message import Message
from .models.local_assistant_message import AssistantMessage
from .models.local_human_task import HumanTask

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

        from .repositories.assistant_message_repository import AssistantMessageRepository
        AssistantMessageRepository()._ensure_columns()
        logger.info("AssistantMessage table migration complete")

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
