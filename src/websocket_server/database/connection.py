# 数据库连接管理
import os
import sqlite3
import logging
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
    """数据库连接管理器"""
    _instance = None
    _connections = {}
    _pools = {}
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ConnectionManager, cls).__new__(cls)
            cls._instance._init_connections()
        return cls._instance
    
    def _init_connections(self):
        """初始化数据库连接"""
        # 在运行时读取环境变量，而不是在模块导入时
        db_config = {
            'default': {
                'ENGINE': os.getenv('DB_ENGINE', 'sqlite'),  # 支持sqlite, mysql, postgresql
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
                    self._connections[db_name] = self._create_sqlite_connection(config)
                elif config['ENGINE'] == 'mysql':
                    self._pools[db_name] = self._create_mysql_pool(config)
                # 可以扩展支持其他数据库类型
                # elif config['ENGINE'] == 'postgresql':
                #     self._connections[db_name] = self._create_postgresql_connection(config)
                else:
                    logger.error(f"不支持的数据库类型: {config['ENGINE']}")
            except Exception as e:
                logger.error(f"初始化数据库连接失败: {str(e)}")
                raise DatabaseConnectionError(f"初始化数据库连接失败: {str(e)}")
    
    def _create_sqlite_connection(self, config: Dict[str, Any]):
        """创建SQLite连接"""
        try:
            # 使用绝对路径确保数据库文件在项目根目录
            import os
            db_path = config['NAME']
            if not os.path.isabs(db_path):
                # 获取项目根目录（假设database目录在项目根目录下）
                project_root = os.path.dirname(os.path.dirname(__file__))
                db_path = os.path.join(project_root, db_path)
            
            conn = sqlite3.connect(db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row  # 使查询结果可以通过列名访问
            # WAL 模式 + busy_timeout：读写不互斥（多读单写），写入期间读不阻塞；
            # busy_timeout 让并发连接在锁冲突时等待而非立即抛 SQLITE_BUSY；
            # synchronous=NORMAL 配合 WAL 在安全性与吞吐间取平衡。
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA synchronous=NORMAL")
            except Exception as e:
                logger.warning(f"设置 SQLite PRAGMA 失败（不影响运行）: {e}")
            logger.info(f"创建SQLite连接成功: {db_path}")
            return conn
        except Exception as e:
            logger.error(f"创建SQLite连接失败: {str(e)}")
            raise DatabaseConnectionError(f"创建SQLite连接失败: {str(e)}")
    
    def _create_mysql_pool(self, config: Dict[str, Any]):
        """创建MySQL连接池"""
        try:
            # 在运行时读取连接池配置
            pool_config = {
                'max_connections': int(os.getenv('DB_MAX_CONNECTIONS', 10)),
                'min_connections': int(os.getenv('DB_MIN_CONNECTIONS', 1)),
                'timeout': int(os.getenv('DB_TIMEOUT', 30)),
            }
            
            # 使用DBUtils的PooledDB创建连接池
            pool = PooledDB(
                creator=pymysql,  # 使用pymysql作为连接创建器
                maxconnections=pool_config['max_connections'],  # 最大连接数
                mincached=pool_config['min_connections'],  # 最小空闲连接数
                blocking=False,  # 连接池满时不阻塞，立即抛出异常
                maxusage=None,  # 连接最大使用次数
                setsession=["SET time_zone = '+00:00'"],  # 会话设置，设置时区为 UTC
                ping=0,  # 不主动ping
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
        """获取数据库连接"""
        conn = None
        is_pool_connection = False
        
        try:
            # 检查是否有连接池
            if db_name in self._pools:
                try:
                    conn = self._pools[db_name].connection()  # 从DBUtils连接池获取连接
                    is_pool_connection = True
                    logger.debug(f"从连接池获取连接: {db_name}")
                except Exception as pool_error:
                    logger.error(f"从连接池获取连接失败: {str(pool_error)}")
                    raise DatabaseConnectionError(f"数据库连接池已耗尽，请稍后再试。错误: {str(pool_error)}")
            elif db_name in self._connections:
                conn = self._connections[db_name]
                logger.debug(f"获取已有连接: {db_name}")
            else:
                logger.error(f"未找到数据库连接: {db_name}")
                raise DatabaseConnectionError(f"未找到数据库连接: {db_name}")
            
            yield conn

            # 如果是连接池连接，提交事务（如果仍有活动事务）
            if is_pool_connection and conn:
                try:
                    conn.commit()
                except Exception as commit_error:
                    # 忽略"无活动事务"的错误（可能已在with块内提交）
                    error_str = str(commit_error).lower()
                    if "no transaction" not in error_str and "事务" not in error_str:
                        logger.error(f"提交事务失败: {str(commit_error)}")
                        raise
        except Exception as e:
            # 发生异常时回滚事务
            if is_pool_connection and conn:
                try:
                    conn.rollback()
                except Exception as rollback_error:
                    # 忽略"无活动事务"的错误
                    error_str = str(rollback_error).lower()
                    if "no transaction" not in error_str and "事务" not in error_str:
                        logger.error(f"回滚事务失败: {str(rollback_error)}")
            logger.error(f"数据库操作异常: {str(e)}")
            raise
        finally:
            # 如果是连接池连接，关闭连接（归还到连接池）
            if is_pool_connection and conn:
                try:
                    conn.close()  # DBUtils的连接池使用close()方法归还连接，而不是release()
                    logger.debug(f"连接已归还到连接池: {db_name}")
                except Exception as close_error:
                    logger.error(f"归还连接到连接池失败: {str(close_error)}")
    
    def close_all(self):
        """关闭所有连接"""
        # 关闭普通连接
        for db_name, conn in self._connections.items():
            try:
                conn.close()
                logger.info(f"关闭数据库连接: {db_name}")
            except Exception as e:
                logger.error(f"关闭数据库连接失败: {db_name}, {str(e)}")
        
        # 清空连接字典
        self._connections = {}
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