# Event API endpoints — query local events table (platform's only persistent store)
from flask import request
from flask_restx import Resource, fields, Namespace

from database.repositories.event_repository import EventRepository


def register_event_routes(app):
    """Register event API routes (local events table — platform's sync target)."""
    api = app.config.get("RESTX_API")

    event_ns = Namespace("event", description="Event log API (local)")
    api.add_namespace(event_ns, path="/api/event")

    @event_ns.route("")
    class EventList(Resource):
        @event_ns.doc("list_events")
        @event_ns.param("event_type", "Filter by event type prefix")
        @event_ns.param("limit", "Maximum number of events", default=100)
        def get(self):
            """List recent events from local table."""
            event_type = request.args.get("event_type")
            limit = int(request.args.get("limit", 100))

            event_repo = EventRepository()
            if event_type:
                events = event_repo.find_by_event_type_prefix(event_type, limit=limit)
            else:
                events = event_repo.find_recent(limit=limit)

            return [e.to_dict() for e in events]