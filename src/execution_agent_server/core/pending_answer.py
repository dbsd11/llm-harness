# Pending Answer Store - thread-safe blocking store for ask_assistant requests.
#
# When the execution agent calls the ask_assistant tool, it registers a pending
# request here and blocks until the backend routes the assistant's response back.
import threading
import uuid
from typing import Dict, Optional
from logger import logger


class _PendingRequest:
    __slots__ = ("question", "event", "answer", "success")

    def __init__(self, question: str):
        self.question = question
        self.event = threading.Event()
        self.answer = ""
        self.success = False


class PendingAnswerStore:
    """Thread-safe store for blocking ask_assistant calls.

    Usage:
        # In the tool (agent thread):
        request_id = store.register("What is the device model?")
        ws_client.send(ask_assistant_request_frame(..., request_id, ...))
        answer, success = store.wait(request_id, timeout=300)

        # In the WS client (recv thread):
        store.resolve(request_id, answer, success)
    """

    def __init__(self):
        self._requests: Dict[str, _PendingRequest] = {}
        self._lock = threading.Lock()

    def register(self, question: str) -> str:
        """Register a pending request and return its request_id."""
        request_id = str(uuid.uuid4())
        req = _PendingRequest(question)
        with self._lock:
            self._requests[request_id] = req
        logger.info(f"Registered ask_assistant request {request_id}: "
                    f"{question[:80]}...")
        return request_id

    def wait(self, request_id: str, timeout: float = 300) -> tuple:
        """Block until the response arrives or timeout.

        Returns (answer: str, success: bool).
        Removes the request from the store regardless of outcome.
        """
        with self._lock:
            req = self._requests.get(request_id)
        if req is None:
            logger.warning(f"wait: unknown request_id {request_id}")
            return ("", False)

        resolved = req.event.wait(timeout=timeout)
        with self._lock:
            self._requests.pop(request_id, None)

        if resolved:
            logger.info(f"ask_assistant request {request_id} answered "
                        f"({len(req.answer)} chars)")
            return (req.answer, req.success)
        else:
            logger.warning(f"ask_assistant request {request_id} timed out "
                          f"after {timeout}s")
            return ("[error] ask_assistant timed out", False)

    def resolve(self, request_id: str, answer: str, success: bool) -> None:
        """Resolve a pending request with the assistant's answer.

        Called from the WS recv thread when an ask_assistant_response frame
        arrives.
        """
        with self._lock:
            req = self._requests.get(request_id)
        if req is None:
            logger.warning(f"resolve: unknown or expired request_id "
                          f"{request_id}")
            return
        req.answer = answer
        req.success = success
        req.event.set()
        logger.info(f"Resolved ask_assistant request {request_id} "
                    f"(success={success})")

    def cancel(self, request_id: str) -> None:
        """Cancel a pending request (e.g. on disconnect)."""
        with self._lock:
            req = self._requests.pop(request_id, None)
        if req:
            req.answer = "[error] request cancelled"
            req.success = False
            req.event.set()


pending_answers = PendingAnswerStore()
