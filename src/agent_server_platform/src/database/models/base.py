# 基础模型类
from datetime import datetime
from typing import Dict, Any, List, Optional, ClassVar, Type
import os

class BaseModel:
    """基础模型类，所有数据模型的父类"""
    # 表名
    __tablename__: ClassVar[str] = ""
    
    # 主键字段
    __primary_key__: ClassVar[str] = "id"
    
    # 字段定义
    __fields__: ClassVar[Dict[str, Type]] = {}
    
    # 默认排序
    __default_order__: ClassVar[str] = ""
    
    # 添加属性访问器
    @property
    def table_name(self) -> str:
        return self.__tablename__
    
    @classmethod
    @property
    def table_name(cls) -> str:
        return cls.__tablename__
    
    @property
    def primary_key(self) -> str:
        return self.__primary_key__
    
    @classmethod
    @property
    def primary_key(cls) -> str:
        return cls.__primary_key__
    
    def __init__(self, **kwargs):
        """初始化模型实例"""
        for field_name, field_type in self.__fields__.items():
            if field_name in kwargs:
                setattr(self, field_name, kwargs[field_name])
            else:
                setattr(self, field_name, None)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'BaseModel':
        """从字典创建模型实例"""
        # 处理datetime字段转换
        processed_data = {}
        for field_name, field_value in data.items():
            if field_name in cls.__fields__ and cls.__fields__[field_name] == datetime:
                # 如果是datetime字段且值为字符串，转换为datetime对象
                if isinstance(field_value, str):
                    try:
                        field_value = datetime.fromisoformat(field_value.replace('Z', '+00:00'))
                    except (ValueError, AttributeError):
                        try:
                            # 尝试其他常见格式
                            field_value = datetime.strptime(field_value, '%Y-%m-%d %H:%M:%S')
                        except ValueError:
                            try:
                                field_value = datetime.strptime(field_value, '%Y-%m-%d %H:%M:%S.%f')
                            except ValueError:
                                # 如果转换失败，保持原值
                                pass
            processed_data[field_name] = field_value
        
        return cls(**processed_data)
    
    def to_dict(self) -> Dict[str, Any]:
        """将模型实例转换为字典"""
        result = {}
        for field_name in self.__fields__.keys():
            field_value = getattr(self, field_name)
            if field_value is not None:
                # Convert datetime to ISO string for JSON serialization
                if isinstance(field_value, datetime):
                    field_value = field_value.isoformat()
                result[field_name] = field_value
        return result
    
    @classmethod
    def get_create_table_sql(cls) -> str:
        """获取创建表的SQL语句"""
        fields = []
        engine = os.getenv('DB_ENGINE', 'sqlite')
        
        for field_name, field_type in cls.__fields__.items():
            field_def = f"{field_name} "
            
            if engine == 'sqlite':
                if field_type == int:
                    field_def += "INTEGER"
                elif field_type == float:
                    field_def += "REAL"
                elif field_type == bool:
                    field_def += "BOOLEAN"
                elif field_type == datetime:
                    field_def += "TIMESTAMP"
                else:
                    field_def += "TEXT"
                
                if field_name == cls.__primary_key__:
                    field_def += " PRIMARY KEY"
                    if field_type == int:
                        field_def += " AUTOINCREMENT"
            
            elif engine == 'mysql':
                if field_type == int:
                    field_def += "INT"
                elif field_type == float:
                    field_def += "FLOAT"
                elif field_type == bool:
                    field_def += "TINYINT(1)"
                elif field_type == datetime:
                    field_def += "DATETIME"
                elif field_type == str and field_name.endswith('_text'):
                    field_def += "TEXT"
                else:
                    field_def += "VARCHAR(255)"
                
                if field_name == cls.__primary_key__:
                    field_def += " PRIMARY KEY"
                    if field_type == int:
                        field_def += " AUTO_INCREMENT"
            
            fields.append(field_def)
        
        fields_str = ", ".join(fields)
        
        if engine == 'mysql':
            return f"CREATE TABLE IF NOT EXISTS {cls.__tablename__} ({fields_str}) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;"
        else:
            return f"CREATE TABLE IF NOT EXISTS {cls.__tablename__} ({fields_str});"