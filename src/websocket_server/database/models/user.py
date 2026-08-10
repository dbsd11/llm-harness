# 用户模型
from datetime import datetime
from typing import Dict, Any

from .base import BaseModel

class User(BaseModel):
    """用户模型"""
    __tablename__ = "users"
    __primary_key__ = "id"
    __fields__ = {
        "id": int,
        "username": str,
        "email": str,
        "password_hash": str,
        "is_active": bool,
        "organization_id": int,  # 关联的单位ID
        "created_at": datetime,
        "updated_at": datetime
    }
    __default_order__ = "id DESC"
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)