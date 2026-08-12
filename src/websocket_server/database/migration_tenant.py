"""Tenant ID 列迁移脚本

为所有表添加 tenant_id 列（如果不存在），支持多租户隔离。
沿用现有 _ensure_columns() 模式（ALTER TABLE ADD COLUMN + 错误抑制）。
"""
from .connection import get_connection_manager
from logger import logger


# 需要添加 tenant_id 的所有表
TABLES_WITH_TENANT_ID = [
    "execution_servers",
    "tasks",
    "scenarios",
    "messages",
    "events",
    "agents",
    "consumer_offsets",
    "assistant_messages",
    "human_tasks",
    "workflows",
    "workflow_task_templates",
    "users",
]


def migrate_add_tenant_id():
    """为所有表添加 tenant_id 列和索引"""
    cm = get_connection_manager()

    with cm.get_connection() as conn:
        cursor = conn.cursor()

        for table in TABLES_WITH_TENANT_ID:
            # 检查列是否已存在
            try:
                cursor.execute(f"PRAGMA table_info({table})")
                existing_columns = {row["name"] for row in cursor.fetchall()}
            except Exception:
                # MySQL 或其他数据库，尝试直接添加
                existing_columns = set()

            if "tenant_id" not in existing_columns:
                try:
                    cursor.execute(f"ALTER TABLE {table} ADD COLUMN tenant_id TEXT")
                    logger.info(f"Added tenant_id column to {table}")
                except Exception as e:
                    # 列可能已存在（并发迁移），忽略错误
                    if "duplicate column" not in str(e).lower() and "already exists" not in str(e).lower():
                        logger.warning(f"Failed to add tenant_id to {table}: {e}")

        conn.commit()

    # 创建索引（单独事务，避免长事务）
    with cm.get_connection() as conn:
        cursor = conn.cursor()
        for table in TABLES_WITH_TENANT_ID:
            index_name = f"idx_{table}_tenant"
            try:
                cursor.execute(
                    f"CREATE INDEX IF NOT EXISTS {index_name} ON {table}(tenant_id)"
                )
            except Exception as e:
                logger.warning(f"Failed to create index {index_name}: {e}")
        conn.commit()

    logger.info("Tenant ID migration complete")


def migrate_set_default_tenant(default_tenant_id: str):
    """将现有无 tenant_id 的数据分配给默认租户

    用于一次性数据迁移，将升级前存在的数据分配给指定租户。

    Args:
        default_tenant_id: 默认租户 ID
    """
    if not default_tenant_id:
        logger.warning("No default tenant ID provided, skipping data migration")
        return

    cm = get_connection_manager()
    with cm.get_connection() as conn:
        cursor = conn.cursor()
        for table in TABLES_WITH_TENANT_ID:
            try:
                cursor.execute(
                    f"UPDATE {table} SET tenant_id = ? WHERE tenant_id IS NULL",
                    (default_tenant_id,)
                )
                affected = cursor.rowcount
                if affected > 0:
                    logger.info(f"Migrated {affected} rows in {table} to tenant {default_tenant_id}")
            except Exception as e:
                logger.warning(f"Failed to migrate data in {table}: {e}")
        conn.commit()

    logger.info("Default tenant data migration complete")
