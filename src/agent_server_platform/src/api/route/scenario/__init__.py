# Scenario API endpoints — thin proxy to ws_server HTTP API
from flask import request
from flask_restx import Resource, fields, Namespace

from api_client import api_client


def register_scenario_routes(app):
    """Register scenario API routes (proxied to ws_server)."""
    api = app.config.get("RESTX_API")

    scenario_ns = Namespace("scenario", description="Scenario management API (proxied)")
    api.add_namespace(scenario_ns, path="/api/scenario")

    create_scenario_model = api.model("CreateScenario", {
        "scenario_type": fields.String(required=True, description="Scenario type"),
        "name": fields.String(required=True, description="Scenario name"),
        "description": fields.String(description="Description"),
        "config": fields.Raw(description="Configuration (JSON)"),
    })

    @scenario_ns.route("")
    class ScenarioList(Resource):
        @scenario_ns.doc("list_scenarios")
        def get(self):
            """List all scenarios (proxied)."""
            resp = api_client.list_scenarios(
                state=request.args.get("state"),
                limit=int(request.args.get("limit", 100)),
            )
            return resp.get("scenarios", [])

        @scenario_ns.doc("create_scenario")
        @scenario_ns.expect(create_scenario_model)
        def post(self):
            """Create new scenario (proxied)."""
            data = request.get_json() or {}
            resp = api_client.create_scenario(
                scenario_type=data.get("scenario_type"),
                name=data.get("name", ""),
                description=data.get("description", ""),
                config=data.get("config", {}),
            )
            if resp.get("success"):
                return {"scenario_id": resp["scenario"].get("scenario_id", ""), "status": "created"}, 201
            return {"error": resp.get("error", "Unknown error")}, 400

    @scenario_ns.route("/<string:scenario_id>")
    class ScenarioInstance(Resource):
        @scenario_ns.doc("get_scenario")
        def get(self, scenario_id):
            """Get scenario details (proxied)."""
            resp = api_client.get_scenario(scenario_id)
            if resp.get("success"):
                return resp["scenario"]
            return {"error": resp.get("error", "Not found")}, 404

        @scenario_ns.doc("start_scenario")
        def post(self, scenario_id):
            """Start scenario (proxied)."""
            resp = api_client.start_scenario(scenario_id)
            if resp.get("success"):
                return {"status": "started", "scenario_id": scenario_id}
            return {"error": resp.get("error", "Failed")}, 500

        @scenario_ns.doc("stop_scenario")
        def delete(self, scenario_id):
            """Stop scenario (proxied)."""
            resp = api_client.stop_scenario(scenario_id)
            if resp.get("success"):
                return {"status": "stopped", "scenario_id": scenario_id}
            return {"error": resp.get("error", "Not found")}, 404