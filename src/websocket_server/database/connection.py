# 数据库连接管理
import os
import sqlite3
import logging
import threading
from contextlib import contextmanager
from typing import Dict, Any, Optional
import pymysql
import pymysql.cursors
# 替换为DBUtils
from dbutils.pooled_db import PooledDB

logger = logging.getLogger(__name__)

class DatabaseConnectionError(Exception):
    """数据库连接异常"""
    pass

class ConnectionManager:
    """数据库连接管理器

    所有数据库引擎（SQLite / MySQL）均通过 PooledDB 提供连接池，
    消除原先 SQLite 单连接 + 全局 threading.Lock 的串行化瓶颈。
    """
    _instance = None
    _pools = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ConnectionManager, cls).__new__(cls)
            cls._instance._init_connections()
        return cls._instance

    def _init_connections(self):
        """初始化数据库连接"""
        db_config = {
            'default': {
                'ENGINE': os.getenv('DB_ENGINE', 'sqlite'),
                'NAME': os.getenv('DB_NAME', 'cnipy_app.db'),
                'USER': os.getenv('DB_USER', ''),
                'PASSWORD': os.getenv('DB_PASSWORD', ''),
                'HOST': os.getenv('DB_HOST', ''),
                'PORT': os.getenv('DB_PORT', ''),
            }
        }

        for db_name, config in db_config.items():
            try:
                if config['ENGINE'] == 'sqlite':
                    self._pools[db_name] = self._create_sqlite_pool(config)
                elif config['ENGINE'] == 'mysql':
                    self._pools[db_name] = self._create_mysql_pool(config)
                else:
                    logger.error(f"不支持的数据库类型: {config['ENGINE']}")
            except Exception as e:
                logger.error(f"初始化数据库连接失败: {str(e)}")
                raise DatabaseConnectionError(f"初始化数据库连接失败: {str(e)}")

    def _create_sqlite_pool(self, config: Dict[str, Any]):
        """通过 DBUtils PooledDB 创建 SQLite 连接池。

        每个池连接独立启用 WAL / NORMAL sync / busy_timeout，
        读写并发不再受全局锁串行化限制。
        """
        try:
            db_path = config['NAME']
            if not os.path.isabs(db_path):
                project_root = os.path.dirname(os.path.dirname(__file__))
                db_path = os.path.join(project_root, db_path)

            pool_size = int(os.getenv("SQLITE_POOL_SIZE",
                                      os.getenv("DB_THREAD_POOL_SIZE", "10")))

            pool = PooledDB(
                creator=sqlite3,
                maxconnections=pool_size,
                mincached=1,
                blocking=True,
                maxusage=None,
                setsession=[
                    "PRAGMA journal_mode=WAL",
                    "PRAGMA busy_timeout=5000",
                    "PRAGMA synchronous=NORMAL",
                ],
                ping=0,
                database=db_path,
                check_same_thread=False,
                timeout=5,
            )

            # 初始化后立即取一次连接以设置 row_factory（PooledDB 的 setsession
            # 只接受 SQL 字符串，row_factory 需要通过 Python 赋值）。后续从池中
            # 取出的连接已是同一对象，row_factory 随之生效。
            init_conn = pool.connection()
            init_conn.row_factory = sqlite3.Row
            init_conn.close()  # 归还到池中，而非真正关闭

            logger.info(f"创建SQLite连接池成功: {db_path} (pool_size={pool_size})")
            return pool
        except Exception as e:
            logger.error(f"创建SQLite连接池失败: {str(e)}")
            raise DatabaseConnectionError(f"创建SQLite连接池失败: {str(e)}")

    def _create_mysql_pool(self, config: Dict[str, Any]):
        """创建MySQL连接池"""
        try:
            pool_config = {
                'max_connections': int(os.getenv('DB_MAX_CONNECTIONS', 10)),
                'min_connections': int(os.getenv('DB_MIN_CONNECTIONS', 1)),
                'timeout': int(os.getenv('DB_TIMEOUT', 30)),
            }

            pool = PooledDB(
                creator=pymysql,
                maxconnections=pool_config['max_connections'],
                mincached=pool_config['min_connections'],
                blocking=False,
                maxusage=None,
                setsession=["SET time_zone = '+00:00'"],
                ping=0,
                host=config['HOST'],
                user=config['USER'],
                password=config['PASSWORD'],
                database=config['NAME'],
                port=int(config['PORT']) if config['PORT'] else 3306,
                charset='utf8mb4',
                cursorclass=pymysql.cursors.DictCursor
            )

            logger.info(f"创建MySQL连接池成功: {config['NAME']}")
            return pool
        except Exception as e:
            logger.error(f"创建MySQL连接池失败: {str(e)}")
            raise DatabaseConnectionError(f"创建MySQL连接池失败: {str(e)}")

    @contextmanager
    def get_connection(self, db_name: str = 'default'):
        """从连接池获取连接（SQLite / MySQL 统一走池，无全局锁）。"""
        conn = None
        is_pool_connection = False

        try:
            if db_name in self._pools:
                conn = self._pools[db_name].connection()
                is_pool_connection = True
                logger.debug(f"从连接池获取连接: {db_name}")
            else:
                logger.error(f"未找到数据库连接: {db_name}")
                raise DatabaseConnectionError(f"未找到数据库连接: {db_name}")

            yield conn

            # 提交事务（如果仍有活动事务）
            if is_pool_connection and conn:
                try:
                    conn.commit()
                except Exception as commit_error:
                    error_str = str(commit_error).lower()
                    if "no transaction" not in error_str and "事务" not in error_str:
                        logger.error(f"提交事务失败: {str(commit_error)}")
                        raise
        except Exception as e:
            if is_pool_connection and conn:
                try:
                    conn.rollback()
                except Exception as rollback_error:
                    error_str = str(rollback_error).lower()
                    if "no transaction" not in error_str and "事务" not in error_str:
                        logger.error(f"回滚事务失败: {str(rollback_error)}")
            logger.error(f"数据库操作异常: {str(e)}")
            raise
        finally:
            if is_pool_connection and conn:
                try:
                    conn.close()  # 归还到池中
                    logger.debug(f"连接已归还到连接池: {db_name}")
                except Exception as close_error:
                    logger.error(f"归还连接到连接池失败: {str(close_error)}")

    def close_all(self):
        """关闭所有连接"""
        for db_name, pool in self._pools.items():
            try:
                pool.close()
                logger.info(f"关闭数据库连接池: {db_name}")
            except Exception as e:
                logger.error(f"关闭数据库连接池失败: {db_name}, {str(e)}")

        self._pools = {}
        logger.info("已关闭所有数据库连接")

# 全局连接管理器实例 - 延迟初始化
connection_manager = None

def get_connection_manager():
    """获取连接管理器实例（延迟初始化）"""
    global connection_manager
    if connection_manager is None:
        connection_manager = ConnectionManager()
    return connection_manager

def reset_connection_manager():
    """重置连接管理器实例，强制下次调用时重新初始化"""
    global connection_manager
    if connection_manager is not None:
        connection_manager.close_all()
        connection_manager = None
