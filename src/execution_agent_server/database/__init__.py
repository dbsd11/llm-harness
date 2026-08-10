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
