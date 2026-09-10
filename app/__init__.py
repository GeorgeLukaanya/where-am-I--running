"""Application factory for the "Where am I running?" app."""

from flask import Flask


def create_app() -> Flask:
    app = Flask(__name__)

    from app.main import bp

    app.register_blueprint(bp)
    return app
