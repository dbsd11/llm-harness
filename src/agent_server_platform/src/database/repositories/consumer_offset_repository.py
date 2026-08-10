# ConsumerOffset repository
from typing import Optional
from datetime import datetime
from .base_repository import BaseRepository
from ..models.consumer_offset import ConsumerOffset


class ConsumerOffsetRepository(BaseRepository[ConsumerOffset]):
    """Repository for consumer offset tracking (per-BOT consumption position)"""

    def __init__(self):
        super().__init__(ConsumerOffset)

    def get_offset(self, consumer_id: str) -> int:
        """Get last consumed message id for a consumer. Returns 0 if not found."""
        offset = self.find_by_id(consumer_id)
        return offset.last_message_id if offset else 0

    def update_offset(self, consumer_id: str, message_id: int) -> None:
        """Upsert consumer offset to the given message id."""
        existing = self.find_by_id(consumer_id)
        if existing:
            existing.last_message_id = message_id
            existing.updated_at = datetime.now()
            self.update(existing)
        else:
            self.create(ConsumerOffset(
                consumer_id=consumer_id,
                last_message_id=message_id,
                updated_at=datetime.now(),
            ))
