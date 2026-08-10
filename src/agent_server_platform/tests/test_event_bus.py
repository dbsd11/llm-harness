"""
Unit / integration tests for core/event_bus.py

Tests:
- emit returns trace_id
- subscribe/unsubscribe
- handler receives events
- multiple subscribers
- wildcard subscriptions (task.*)
- event persistence to DB
- trace_id propagation
- metadata propagation
- handler exceptions don't crash the bus
"""
import pytest
import time
import json

from core.event_bus import EventBus, event_bus


class TestEventBusEmit:
    def test_emit_returns_trace_id(self, event_bus):
        trace_id = event_bus.emit("test.event", {"key": "value"})
        assert trace_id is not None
        assert isinstance(trace_id, str)

    def test_emit_with_explicit_trace_id(self, event_bus):
        trace_id = event_bus.emit("test.event", {"key": "value"},
                                   trace_id="my-trace-123")
        assert trace_id == "my-trace-123"

    def test_emit_auto_generates_trace_id(self, event_bus):
        t1 = event_bus.emit("test.event", {"n": 1})
        t2 = event_bus.emit("test.event", {"n": 2})
        assert t1 != t2


class TestEventBusSubscribe:
    def test_handler_receives_event(self, event_bus):
        received = []

        def handler(event):
            received.append(event)

        event_bus.subscribe("test.hello", handler)
        event_bus.emit("test.hello", {"greeting": "hi"})

        time.sleep(0.5)

        assert len(received) == 1
        assert received[0]["event_type"] == "test.hello"
        assert received[0]["data"] == {"greeting": "hi"}

        event_bus.unsubscribe("test.hello", handler)

    def test_multiple_subscribers(self, event_bus):
        r1, r2 = [], []

        event_bus.subscribe("test.multi", lambda e: r1.append(e))
        event_bus.subscribe("test.multi", lambda e: r2.append(e))
        event_bus.emit("test.multi", {"x": 1})

        time.sleep(0.5)

        assert len(r1) == 1
        assert len(r2) == 1

    def test_unsubscribe_stops_delivery(self, event_bus):
        received = []
        handler = lambda e: received.append(e)

        event_bus.subscribe("test.unsub", handler)
        event_bus.emit("test.unsub", {"n": 1})
        time.sleep(0.3)

        event_bus.unsubscribe("test.unsub", handler)
        event_bus.emit("test.unsub", {"n": 2})
        time.sleep(0.3)

        assert len(received) == 1

    def test_handler_only_receives_matching_events(self, event_bus):
        received = []
        handler = lambda e: received.append(e)

        event_bus.subscribe("test.specific", handler)
        event_bus.emit("test.other", {"n": 1})
        event_bus.emit("test.specific", {"n": 2})
        time.sleep(0.5)

        assert len(received) == 1
        assert received[0]["data"]["n"] == 2


class TestWildcardSubscription:
    def test_wildcard_catches_matching_prefix(self, event_bus):
        received = []
        handler = lambda e: received.append(e)

        event_bus.subscribe("task.*", handler)
        event_bus.emit("task.created", {"id": "1"})
        event_bus.emit("task.completed", {"id": "2"})
        event_bus.emit("scenario.started", {"id": "3"})

        time.sleep(0.5)

        task_events = [e for e in received if e["event_type"].startswith("task.")]
        assert len(task_events) == 2
        scenario_events = [e for e in received if e["event_type"].startswith("scenario.")]
        assert len(scenario_events) == 0

    def test_wildcard_does_not_match_unrelated_events(self, event_bus):
        received = []
        handler = lambda e: received.append(e)

        event_bus.subscribe("task.*", handler)
        event_bus.emit("agent.registered", {"id": "1"})

        time.sleep(0.3)
        assert len(received) == 0


class TestEventBusPersistence:
    def test_events_are_persisted_to_db(self, event_bus, event_repo):
        event_bus.emit("test.persist", {"data": "persistent"})
        time.sleep(0.5)

        events = event_repo.find_by_event_type("test.persist")
        assert len(events) >= 1
        data = json.loads(events[0].data)
        assert data["data"] == "persistent"

    def test_trace_id_stored_in_db(self, event_bus, event_repo):
        trace_id = event_bus.emit("test.trace", {"x": 1},
                                   trace_id="trace-abc-123")

        time.sleep(0.5)
        events = event_repo.find_by_trace_id("trace-abc-123")
        assert len(events) >= 1
        assert events[0].trace_id == "trace-abc-123"

    def test_metadata_stored_in_db(self, event_bus, event_repo):
        event_bus.emit("test.meta", {"x": 1},
                       metadata={"source": "test"})
        time.sleep(0.5)

        events = event_repo.find_by_event_type("test.meta")
        assert len(events) >= 1
        meta = json.loads(events[0].metadata)
        assert meta["source"] == "test"


class TestEventBusErrorHandling:
    def test_handler_exception_does_not_crash_bus(self, event_bus):
        """A failing handler should not prevent other handlers from working."""
        good_received = []

        def bad_handler(event):
            raise RuntimeError("intentional")

        def good_handler(event):
            good_received.append(event)

        event_bus.subscribe("test.error", bad_handler)
        event_bus.subscribe("test.error", good_handler)
        event_bus.emit("test.error", {"n": 1})

        time.sleep(0.5)

        assert len(good_received) == 1
