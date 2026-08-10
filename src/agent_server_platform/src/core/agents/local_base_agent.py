# Base agent interface and AgentRun
from typing import Any, Dict, Optional
from abc import ABC, abstractmethod
from datetime import datetime
from queue import Queue


class SystemMessageOutput:
    """Queue-based streaming output for agent messages"""

    def __init__(self, maxsize: int = 1000):
        self.message_queue = Queue(maxsize=maxsize)
        self.result_queue = Queue(maxsize=1)

    def emit_message(self, message: str) -> None:
        """Emit intermediate message"""
        try:
            self.message_queue.put_nowait({
                "message": message,
                "timestamp": datetime.now().isoformat()
            })
        except:
            pass  # Queue full, drop message

    def set_result(self, result: Dict[str, Any]) -> None:
        """Set final result"""
        try:
            self.result_queue.put_nowait(result)
        except:
            pass  # Result already set

    def get_message(self, timeout: int = 1) -> Optional[Dict[str, Any]]:
        """Get next message"""
        try:
            return self.message_queue.get(timeout=timeout)
        except:
            return None

    def get_result(self, timeout: int = None) -> Optional[Dict[str, Any]]:
        """Get final result"""
        try:
            return self.result_queue.get(timeout=timeout)
        except:
            return None

    def reset(self) -> None:
        """Reset queues"""
        while not self.message_queue.empty():
            try:
                self.message_queue.get_nowait()
            except:
                break
        while not self.result_queue.empty():
            try:
                self.result_queue.get_nowait()
            except:
                break


class BaseAgent(ABC):
    """Base interface for all agents"""

    @abstractmethod
    def get_agent_type(self) -> str:
        """Return agent type identifier"""
        pass

    @abstractmethod
    def initialize(self, config: Dict[str, Any]) -> None:
        """Initialize agent with configuration"""
        pass

    @abstractmethod
    def run(self, task_id: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute agent logic, return result"""
        pass

    @abstractmethod
    def cleanup(self) -> None:
        """Cleanup resources"""
        pass


class AgentRun:
    """Agent execution instance"""

    def __init__(self, agent: BaseAgent, task_id: str, config: Dict[str, Any]):
        self.agent = agent
        self.task_id = task_id
        self.config = config
        self.processing = False
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        self.started_at: Optional[datetime] = None
        self.completed_at: Optional[datetime] = None
        self.system_message_output = SystemMessageOutput()

    def start(self) -> None:
        """Mark run as started"""
        self.processing = True
        self.started_at = datetime.now()

    def complete(self, result: Dict[str, Any]) -> None:
        """Mark run as completed"""
        self.processing = False
        self.result = result
        self.completed_at = datetime.now()
        self.system_message_output.set_result(result)

    def fail(self, error: str) -> None:
        """Mark run as failed"""
        self.processing = False
        self.error = error
        self.completed_at = datetime.now()

    def reset(self) -> None:
        """Reset run state"""
        self.processing = False
        self.result = None
        self.error = None
        self.started_at = None
        self.completed_at = None
        self.system_message_output.reset()

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "task_id": self.task_id,
            "processing": self.processing,
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }
