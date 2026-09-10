"""Routes reporting where — and in what — this process is running."""

import os
import platform
import socket
import time

from flask import Blueprint, current_app, jsonify, render_template, request

from app.limits import read_limits
from app.metrics import record_visit

bp = Blueprint("main", __name__)

STARTED_AT = time.monotonic()


def build_info() -> dict:
    """Describe the running instance.

    Everything that varies between builds and deployments arrives through the
    environment, so one image can run anywhere without being rebuilt.
    """
    return {
        "hostname": socket.gethostname(),
        "environment": os.getenv("APP_ENV", "development"),
        "image_tag": os.getenv("IMAGE_TAG", "local"),
        "git_sha": os.getenv("GIT_SHA", "unknown"),
        "python_version": platform.python_version(),
        "uptime_seconds": round(time.monotonic() - STARTED_AT, 1),
        **read_limits(),
    }


def visit_info() -> dict:
    """Record this visit and describe where the count is kept."""
    counter = current_app.config["COUNTER"]
    count = counter.increment()
    record_visit(count)
    return {"visits": count, "counter_backend": counter.backend}


@bp.get("/")
def index():
    return render_template("index.html", info={**build_info(), **visit_info()})


@bp.get("/api/info")
def api_info():
    return jsonify({**build_info(), **visit_info()})


MAX_SLEEP_SECONDS = 30


@bp.get("/api/slow")
def slow():
    """Hold the request open, so shutdown draining can be observed.

    Start one of these, then `docker stop` the container: gunicorn stops
    accepting new connections but lets this one finish before the process
    exits. That is a graceful shutdown, and it is the difference between a
    deployment nobody notices and one that drops requests.
    """
    try:
        seconds = float(request.args.get("seconds", 1))
    except ValueError:
        return jsonify({"error": "seconds must be a number"}), 400

    seconds = max(0.0, min(seconds, MAX_SLEEP_SECONDS))
    time.sleep(seconds)
    return jsonify({"slept_seconds": seconds, "hostname": socket.gethostname()})


@bp.get("/health")
def health():
    """Liveness only.

    Deliberately does not touch Redis. If a dependency is unreachable the
    application is still serving, and failing this check would pull a working
    container out of the load balancer for no reason.
    """
    return jsonify({"status": "ok"})
