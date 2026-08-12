# Assistant message model - 助手对话消息
from datetime import datetime
from .base import BaseModel


class AssistantMessage(BaseModel):
    """助手对话消息模型"""
    __tablename__ = "assistant_messages"
    __primary_key__ = "id"
    __fields__ = {
        "id": int,
        "role": str,
        "content": str,
        "timestamp": datetime,
        "session_id": str,
        "summary": str,       # nullable, only for role="summary"
        "tenant_id": str,     # 租户 ID（来自 API Key）
    }
    __default_order__ = "timestamp ASC"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
