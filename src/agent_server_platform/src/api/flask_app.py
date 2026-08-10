# Flask REST API app
from flask import Flask
from flask_restx import Api
from dotenv import load_dotenv
import os
import sys

# Add src to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

load_dotenv()


def create_app():
    """Create Flask app with REST API"""
    app = Flask(__name__)

    # Initialize API
    api = Api(
        app,
        version='1.0',
        title='Agent Server API',
        description='REST API for Agent Server Platform',
        doc='/docs'
    )

    # Store api instance in app config
    app.config['RESTX_API'] = api

    # Register routes
    from api.route import make_route
    make_route(app)

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(
        host=os.getenv("FLASK_HOST", "0.0.0.0"),
        port=int(os.getenv("FLASK_PORT", 5000)),
        debug=True
    )
