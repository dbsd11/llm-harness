# Task API endpoints — thin proxy to ws_server HTTP API
from flask import request
from flask_restx import Resource, fields, Namespace

from api_client import api_client


def register_task_routes(app):
    """Register task API routes (proxied to ws_server)."""
    api = app.config.get("RESTX_API")

    task_ns = Namespace("task", description="Task management API (proxied)")
    api.add_namespace(task_ns, path="/api/task")

    @task_ns.route("")
    class TaskList(Resource):
        @task_ns.doc("list_tasks")
        def get(self):
            """List tasks (proxied)."""
            resp = api_client.list_tasks(
                scenario_id=request.args.get("scenario_id"),
                state=request.args.get("state"),
                limit=int(request.args.get("limit", 100)),
            )
            return resp.get("tasks", [])

    @task_ns.route("/<string:task_id>")
    class TaskInstance(Resource):
        @task_ns.doc("get_task")
        def get(self, task_id):
            """Get task details (proxied)."""
            resp = api_client.get_task(task_id)
            if resp.get("success"):
                return resp["task"]
            return {"error": resp.get("error", "Not found")}, 404

        @task_ns.doc("cancel_task")
        def delete(self, task_id):
            """Cancel task (proxied)."""
            resp = api_client.cancel_task(task_id)
            if resp.get("success"):
                return {"status": "cancelled"}
            return {"error": resp.get("error", "Failed")}, 400

    @task_ns.route("/<string:task_id>/accept")
    class TaskAccept(Resource):
        @task_ns.doc("accept_task")
        def post(self, task_id):
            """Accept/reject task (proxied)."""
            data = request.get_json() or {}
            passed = data.get("passed")
            if passed is None:
                return {"error": "'passed' field is required"}, 400
            feedback = data.get("feedback")
            if not passed and not (feedback and feedback.strip()):
                return {"error": "Feedback required when rejecting"}, 400

            resp = api_client.accept_task(task_id, passed=passed, feedback=feedback or "")
            if resp.get("success"):
                return {"status": "accepted" if passed else "rejected", "task_id": task_id}
            return {"error": resp.get("error", "Review failed")}, 500