# Event bus - queue-based event system with DB persistence (埋点)
from queue import Queue
from typing import Dict, Any, Callable, List, Optional
from datetime import datetime
import os
import threading
import json
import uuid

from database.repositories.event_repository import EventRepository
from logger import logger


class EventBus:
    """
    In-process event bus with persistence and trace context.

    Evolution path:
    - Phase 1: queue.Queue (single-process) - current
    - Phase 2: Redis pub/sub (multi-process)
    - Phase 3: RabbitMQ/Kafka (distributed)
    """

    def __init__(self):
        self.subscribers: Dict[str, List[Callable]] = {}
        self.event_queue = Queue(maxsize=10000)
        self.event_repo = EventRepository()
        self.running = False

        # Start event processing thread
        self.worker_thread = threading.Thread(target=self._process_events, daemon=True)
        self.worker_thread.start()

    def emit(self, event_type: str, data: Dict[str, Any],
             trace_id: str = None, metadata: Dict[str, Any] = None) -> str:
        """
        Emit event to bus with optional trace context (埋点).

        Args:
            event_type: Event type identifier (e.g., 'task.created')
            data: Event data as dict
            trace_id: Trace ID to link related events (auto-generated if None)
            metadata: Extra context as dict

        Returns:
            The trace_id for this event
        """
        if not trace_id:
            trace_id = str(uuid.uuid4())

        event = {
            "event_type": event_type,
            "data": data,
            "trace_id": trace_id,
            "metadata": metadata or {},
            "timestamp": datetime.now().isoformat(),
        }

        # Persist to DB (skipped when EVENT_PERSIST_DISABLED is set — e.g. the
        # standalone execution-agent-server, which has no backend DB access and
        # reports events over WebSocket instead).
        if not os.getenv("EVENT_PERSIST_DISABLED"):
            try:
                self.event_repo.create_event(
                    event_type,
                    json.dumps(data, ensure_ascii=False),
                    trace_id=trace_id,
                    metadata=json.dumps(metadata or {}, ensure_ascii=False),
                )
            except Exception as e:
                logger.error(f"Failed to persist event: {e}")

        # Queue for processing
        try:
            self.event_queue.put(event, block=True, timeout=1)
        except:
            logger.warning(f"Event queue full, dropping event: {event_type}")

        return trace_id

    def subscribe(self, event_type: str, handler: Callable) -> None:
        """
        Subscribe to event type

        Args:
            event_type: Event type to subscribe to
            handler: Callback function to handle event
        """
        if event_type not in self.subscribers:
            self.subscribers[event_type] = []
        self.subscribers[event_type].append(handler)
        logger.debug(f"Subscribed to event type: {event_type}")

    def unsubscribe(self, event_type: str, handler: Callable) -> None:
        """
        Unsubscribe from event type

        Args:
            event_type: Event type to unsubscribe from
            handler: Callback function to remove
        """
        if event_type in self.subscribers and handler in self.subscribers[event_type]:
            self.subscribers[event_type].remove(handler)

    def _process_events(self) -> None:
        """Process events from queue"""
        self.running = True
        while self.running:
            try:
                event = self.event_queue.get(timeout=1)
                event_type = event["event_type"]

                # Call subscribers
                handlers = self.subscribers.get(event_type, [])
                for handler in handlers:
                    try:
                        handler(event)
                    except Exception as e:
                        logger.error(f"Event handler error: {e}")

                # Also call wildcard subscribers (e.g., 'task.*' for all task events)
                for subscribed_type in self.subscribers.keys():
                    if subscribed_type.endswith('.*'):
                        prefix = subscribed_type[:-2]  # Remove '.*'
                        if event_type.startswith(prefix):
                            for handler in self.subscribers[subscribed_type]:
                                try:
                                    handler(event)
                                except Exception as e:
                                    logger.error(f"Wildcard event handler error: {e}")

            except:
                continue  # Timeout, continue loop

    def stop(self) -> None:
        """Stop event processing"""
        self.running = False
        if self.worker_thread:
            self.worker_thread.join(timeout=5)
        logger.info("Event bus stopped")


# Global event bus instance
event_bus = EventBus()
