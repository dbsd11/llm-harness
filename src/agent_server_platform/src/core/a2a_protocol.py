# A2A Protocol - agent-to-agent communication
import uuid
from queue import Queue
from typing import Dict, Any, Optional
from datetime import datetime
from logger import logger
import threading


class A2AMessage:
    """Agent-to-agent message"""

    def __init__(self, from_agent: str, to_agent: str, message_type: str,
                 payload: Dict[str, Any]):
        self.from_agent = from_agent
        self.to_agent = to_agent
        self.message_type = message_type
        self.payload = payload
        self.timestamp = datetime.now()
        self.message_id = str(uuid.uuid4())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "message_id": self.message_id,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "message_type": self.message_type,
            "payload": self.payload,
            "timestamp": self.timestamp.isoformat(),
        }


class A2AProtocol:
    """
    Agent-to-agent communication protocol.

    Evolution path:
    - Phase 1: in-process queues (single-process) - current
    - Phase 2: HTTP REST API (multi-process)
    - Phase 3: gRPC (cross-cluster, high-performance)
    """

    def __init__(self):
        self.agent_queues: Dict[str, Queue] = {}
        self.lock = threading.Lock()

    def register_agent(self, agent_id: str) -> None:
        """Register agent for messaging"""
        with self.lock:
            self.agent_queues[agent_id] = Queue(maxsize=1000)
            logger.info(f"Agent registered for A2A: {agent_id}")

    def unregister_agent(self, agent_id: str) -> None:
        """Unregister agent"""
        with self.lock:
            if agent_id in self.agent_queues:
                del self.agent_queues[agent_id]
                logger.info(f"Agent unregistered from A2A: {agent_id}")

    def send(self, message: A2AMessage) -> bool:
        """
        Send message to agent.

        Args:
            message: A2A message

        Returns:
            True if sent successfully
        """
        with self.lock:
            queue = self.agent_queues.get(message.to_agent)
            if not queue:
                logger.error(f"Agent not registered: {message.to_agent}")
                return False

            try:
                queue.put(message, block=True, timeout=1)
                return True
            except:
                logger.error(f"Failed to send message to {message.to_agent}")
                return False

    def receive(self, agent_id: str, timeout: int = 10) -> Optional[A2AMessage]:
        """
        Receive message for agent.

        Args:
            agent_id: Agent ID
            timeout: Timeout in seconds

        Returns:
            A2A message or None
        """
        with self.lock:
            queue = self.agent_queues.get(agent_id)
            if not queue:
                raise ValueError(f"Agent not registered: {agent_id}")

            try:
                return queue.get(block=True, timeout=timeout)
            except:
                return None

    def broadcast(self, from_agent: str, message_type: str,
                  payload: Dict[str, Any]) -> None:
        """
        Broadcast message to all agents.

        Args:
            from_agent: Sender agent ID
            message_type: Message type
            payload: Message payload
        """
        message = A2AMessage(from_agent, "*", message_type, payload)

        with self.lock:
            for agent_id, queue in self.agent_queues.items():
                if agent_id != from_agent:
                    try:
                        queue.put(message, block=False)
                    except:
                        pass

        logger.debug(f"Broadcast from {from_agent}: {message_type}")

    def send_request(self, from_agent: str, to_agent: str, message_type: str,
                    payload: Dict[str, Any], timeout: int = 30) -> Optional[A2AMessage]:
        """
        Send request and wait for response.

        Args:
            from_agent: Sender agent ID
            to_agent: Recipient agent ID
            message_type: Message type
            payload: Message payload
            timeout: Timeout in seconds

        Returns:
            Response message or None
        """
        # Create request
        request = A2AMessage(from_agent, to_agent, message_type, payload)

        # Send request
        if not self.send(request):
            return None

        # Wait for response (simplified - in production, use correlation ID)
        return self.receive(from_agent, timeout)

    def list_registered_agents(self) -> list:
        """List all registered agents"""
        with self.lock:
            return list(self.agent_queues.keys())


# Global A2A protocol instance
a2a_protocol = A2AProtocol()
