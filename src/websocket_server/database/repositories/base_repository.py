# 基础仓储类
import os
from typing import Dict, Any, List, Optional, Type, TypeVar, Generic
from datetime import datetime

from ..connection import get_connection_manager
from ..models.base import BaseModel

T = TypeVar('T', bound=BaseModel)

class BaseRepository(Generic[T]):
    """基础仓储类，提供通用的CRUD操作"""
    
    # 在__init__方法中添加占位符设置
    def __init__(self, model_class: Type[T]):
        self.model_class = model_class
        self.table_name = model_class.__tablename__  # 使用__tablename__而不是table_name
        self.primary_key = model_class.__primary_key__  # 同样修改primary_key的获取方式
        # 在运行时获取数据库类型
        self.db_engine = os.getenv('DB_ENGINE', 'sqlite')
        # 根据数据库类型设置占位符
        self.placeholder = '%s' if self.db_engine == 'mysql' else '?'
    
    def create_table_if_not_exists(self):
        """创建表（如果不存在）"""
        sql = self.model_class.get_create_table_sql()
        connection_manager = get_connection_manager()
        with connection_manager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            conn.commit()
    
    def create(self, model: T) -> Any:
        """创建记录"""
        data = model.to_dict()
        
        # 移除ID字段（如果是自增主键）
        if self.primary_key in data and data[self.primary_key] is None:
            del data[self.primary_key]
        
        fields = list(data.keys())
        
        if self.db_engine == 'mysql':
            placeholders = ["%s"] * len(fields)
        else:  # sqlite
            placeholders = ["?"] * len(fields)
            
        values = [data[field] for field in fields]
        
        sql = f"INSERT INTO {self.table_name} ({', '.join(fields)}) VALUES ({', '.join(placeholders)})"
        
        connection_manager = get_connection_manager()
        with connection_manager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, values)
            
            # 获取新插入记录的ID
            last_id = None
            if self.db_engine == 'mysql':
                last_id = cursor.lastrowid
            else:  # sqlite
                last_id = cursor.lastrowid
                
            conn.commit()
            return last_id  # 返回新创建记录的ID
    
    def update(self, entity: T) -> bool:
        """更新记录"""
        data = entity.to_dict()
        if 'updated_at' in data:
            data['updated_at'] = datetime.now()
        
        # 确保有主键值
        if self.primary_key not in data or data[self.primary_key] is None:
            raise ValueError(f"更新记录时必须提供主键值: {self.primary_key}")
        
        primary_key_value = data[self.primary_key]
        del data[self.primary_key]  # 从更新字段中移除主键
        
        if not data:  # 如果没有要更新的字段
            return False
        
        if self.db_engine == 'mysql':
            set_clause = ", ".join([f"{field} = %s" for field in data.keys()])
        else:  # sqlite
            set_clause = ", ".join([f"{field} = ?" for field in data.keys()])
            
        values = list(data.values()) + [primary_key_value]  # 添加WHERE子句的值
        
        if self.db_engine == 'mysql':
            sql = f"UPDATE {self.table_name} SET {set_clause} WHERE {self.primary_key} = %s"
        else:  # sqlite
            sql = f"UPDATE {self.table_name} SET {set_clause} WHERE {self.primary_key} = ?"
        
        connection_manager = get_connection_manager()
        with connection_manager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, values)
            conn.commit()
            return cursor.rowcount > 0
    
    # 其余方法也需要类似修改，替换占位符和适配不同数据库的语法差异
    # 这里只展示部分修改，完整实现需要修改所有方法
    def delete(self, id: Any) -> bool:
        """删除记录"""
        # 使用self.placeholder替代硬编码的?
        sql = f"DELETE FROM {self.table_name} WHERE {self.primary_key} = {self.placeholder}"
        
        connection_manager = get_connection_manager()
        with connection_manager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, (id,))
            conn.commit()
            return cursor.rowcount > 0
    
    def find_by_id(self, id: Any) -> Optional[T]:
        """根据ID查找记录"""
        # 使用self.placeholder替代硬编码的?
        sql = f"SELECT * FROM {self.table_name} WHERE {self.primary_key} = {self.placeholder}"
        
        connection_manager = get_connection_manager()
        with connection_manager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, (id,))
            row = cursor.fetchone()
            
            if row is None:
                return None
            
            # 将行转换为字典
            data = {key: row[key] for key in row.keys()}
            return self.model_class.from_dict(data)
    
    def find_by_ids(self, ids: List[Any]) -> List[T]:
        """根据ID列表批量查找记录"""
        if not ids:
            return []
        
        # 构建IN子句的占位符
        placeholders = [self.placeholder] * len(ids)
        placeholders_str = ", ".join(placeholders)
        
        sql = f"SELECT * FROM {self.table_name} WHERE {self.primary_key} IN ({placeholders_str})"
        
        connection_manager = get_connection_manager()
        with connection_manager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, ids)
            rows = cursor.fetchall()
            
            result = []
            for row in rows:
                # 将行转换为字典
                data = {key: row[key] for key in row.keys()}
                result.append(self.model_class.from_dict(data))
            
            return result
    
    def find_all(self, organization_id: int = None, order_by: str = None, limit: int = None, offset: int = None) -> List[T]:
        """查找所有记录"""
        if organization_id:
            sql = f"SELECT * FROM {self.table_name} WHERE organization_id = {organization_id}"
        else:
            sql = f"SELECT * FROM {self.table_name}"
        
        # 添加排序
        if order_by:
            sql += f" ORDER BY {order_by}"
        elif self.model_class.__default_order__:
            sql += f" ORDER BY {self.model_class.__default_order__}"
        
        # 添加分页
        if limit is not None:
            sql += f" LIMIT {limit}"
            if offset is not None:
                sql += f" OFFSET {offset}"
        
        connection_manager = get_connection_manager()
        with connection_manager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            rows = cursor.fetchall()
            
            result = []
            for row in rows:
                # 将行转换为字典
                data = {key: row[key] for key in row.keys()}
                result.append(self.model_class.from_dict(data))
            
            return result
    
    def find_by_criteria(self, criteria: Dict[str, Any], order_by: str = None, 
                         limit: int = None, offset: int = None) -> List[T]:
        """根据条件查找记录"""
        if not criteria:
            return self.find_all(None, order_by, limit, offset)
        
        where_clauses = []
        values = []
        
        for field, value in criteria.items():
            # 判断value类型，如果是dict，则使用dict中的operator和condition
            if isinstance(value, dict) and "operator" in value and "condition" in value:
                # 支持的操作符: >, <, >=, <=, !=, LIKE, IN
                operator = value["operator"]
                condition = value["condition"]
                
                if operator.upper() == "IN" and isinstance(condition, list):
                    # 处理 IN 操作符，需要多个占位符
                    placeholders = ", ".join([self.placeholder] * len(condition))
                    where_clauses.append(f"{field} IN ({placeholders})")
                    values.extend(condition)
                else:
                    # 处理其他操作符
                    where_clauses.append(f"{field} {operator} {self.placeholder}")
                    values.append(condition)
            else:
                # 默认使用等于操作符
                where_clauses.append(f"{field} = {self.placeholder}")
                values.append(value)
        
        where_clause = " AND ".join(where_clauses)
        sql = f"SELECT * FROM {self.table_name} WHERE {where_clause}"
        
        # 添加排序
        if order_by:
            sql += f" ORDER BY {order_by}"
        elif self.model_class.__default_order__:
            sql += f" ORDER BY {self.model_class.__default_order__}"
        
        # 添加分页
        if limit is not None:
            sql += f" LIMIT {limit}"
            if offset is not None:
                sql += f" OFFSET {offset}"
        
        connection_manager = get_connection_manager()
        with connection_manager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, values)
            rows = cursor.fetchall()
            
            result = []
            for row in rows:
                # 将行转换为字典
                data = {key: row[key] for key in row.keys()}
                result.append(self.model_class.from_dict(data))
            
            return result
    
    def count(self, criteria: Dict[str, Any] = None) -> int:
        """计算记录数量"""
        sql = f"SELECT COUNT(*) as count FROM {self.table_name}"
        values = []
        
        if criteria:
            where_clauses = []
            for field, value in criteria.items():
                where_clauses.append(f"{field} = {self.placeholder}")
                values.append(value)
            
            where_clause = " AND ".join(where_clauses)
            sql += f" WHERE {where_clause}"
        
        connection_manager = get_connection_manager()
        with connection_manager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, values)
            row = cursor.fetchone()
            return row['count'] if row else 0
    
    def find_random(self, criteria: Dict[str, Any] = None, limit: int = 10, organization_id: int = None) -> List[T]:
        """随机查询指定数量的记录"""
        # 根据数据库类型使用不同的随机查询语法
        if self.db_engine == 'mysql':
            order_clause = "ORDER BY RAND()"
        else:  # sqlite
            order_clause = "ORDER BY RANDOM()"
        
        # 添加查询条件
        where_clauses = ["1=1"]
        values = []
        if criteria:
            for field, value in criteria.items():
                where_clauses.append(f"{field} = {self.placeholder}")
                values.append(value)

        if organization_id:
            where_clauses.append(f"organization_id = {self.placeholder}")
            values.append(organization_id)

        where_clause = " AND ".join(where_clauses)
        sql = f"SELECT * FROM {self.table_name} WHERE {where_clause} {order_clause} LIMIT {limit}"
        
        connection_manager = get_connection_manager()
        with connection_manager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, values)
            rows = cursor.fetchall()
            
            result = []
            for row in rows:
                # 将行转换为字典
                data = {key: row[key] for key in row.keys()}
                result.append(self.model_class.from_dict(data))
            
            return result