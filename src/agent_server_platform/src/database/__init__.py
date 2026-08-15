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

        _migrate_tenant_id(models)

        return True
    except Exception as e:
        logger.error(f"Failed to initialize database: {str(e)}")
        return False


def _migrate_tenant_id(models: List[Type[BaseModel]]):
    """Add tenant_id column + index to all tables, backfill existing rows."""
    from core.local_tenant import get_tenant_id
    tenant_id = get_tenant_id()

    with get_connection_manager().get_connection() as conn:
        cursor = conn.cursor()

        for model_class in models:
            table = model_class.__tablename__
            try:
                cursor.execute(f"PRAGMA table_info({table})")
                columns = {row[1] for row in cursor.fetchall()}
                if "tenant_id" not in columns:
                    cursor.execute(f"ALTER TABLE {table} ADD COLUMN tenant_id TEXT")
                    logger.info(f"Added tenant_id column to {table}")
            except Exception as e:
                logger.warning(f"Failed to add tenant_id to {table}: {e}")

            try:
                cursor.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_tenant ON {table}(tenant_id)"
                )
            except Exception as e:
                logger.warning(f"Failed to create index on {table}: {e}")

            if tenant_id:
                try:
                    cursor.execute(
                        f"UPDATE {table} SET tenant_id = ? WHERE tenant_id IS NULL",
                        (tenant_id,)
                    )
                    if cursor.rowcount > 0:
                        logger.info(f"Backfilled {cursor.rowcount} rows in {table} with tenant {tenant_id}")
                except Exception as e:
                    logger.warning(f"Failed to backfill {table}: {e}")

        conn.commit()
    logger.info("Tenant ID migration complete")


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
