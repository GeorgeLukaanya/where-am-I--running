"""Application factory for the "Where am I running?" app."""

from flask import Flask

from app import metrics
from app.store import make_counter


def create_app() -> Flask:
    app = Flask(__name__)

    # One counter per process, not one per request: a fresh Redis client on
    # every request would throw away the connection pool.
    app.config["COUNTER"] = make_counter()

    from app.main import bp

    app.register_blueprint(bp)
    metrics.install(app)
    return app
