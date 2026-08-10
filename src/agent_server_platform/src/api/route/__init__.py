# API route registration — thin proxies to ws_server HTTP API
from api.route.task import register_task_routes
from api.route.scenario import register_scenario_routes
from api.route.event import register_event_routes


def make_route(app):
    """Register all API routes (proxied to ws_server)."""
    register_task_routes(app)
    register_scenario_routes(app)
    register_event_routes(app)