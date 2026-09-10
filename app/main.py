"""Routes reporting where — and in what — this process is running."""

import os
import platform
import socket
import time

from flask import Blueprint, current_app, jsonify, render_template, request

from app import fleet
from app.identity import accent_slot, assign_slots
from app.limits import read_limits
from app.metrics import record_visit
from app.telemetry import current_registry, read_counters

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
    info = {**build_info(), **visit_info()}
    # Tint server-side so the page is already the right colour on first paint,
    # and still correct with JavaScript switched off. This is the instance's
    # preferred slot; the fleet view may move it to avoid a collision.
    return render_template("index.html", info=info, slot=accent_slot(info["hostname"]))


@bp.get("/api/info")
def api_info():
    return jsonify({**build_info(), **visit_info()})


def instance_report() -> dict:
    """Everything one instance can say about itself, without changing anything.

    Deliberately does not record a visit and is excluded from request metrics:
    the console polls this on a timer, and an observer that shows up in its own
    measurements is not an observer.
    """
    counter = current_app.config["COUNTER"]
    return {
        **build_info(),
        "counters": read_counters(current_registry(current_app)),
        "visits": counter.read(),
        "counter_backend": counter.backend,
    }


@bp.get("/api/instance")
def instance():
    return jsonify(instance_report())


@bp.get("/api/fleet")
def fleet_view():
    """Every instance behind the configured service name, including this one.

    Peers are resolved fresh on each call rather than cached, so scaling up or
    down is reflected within one poll and nothing has to be told about it.
    """
    port = int(os.getenv("PEER_PORT", "8000"))
    peers = fleet.discover(port=port)
    mine = socket.gethostname()

    reports = fleet.poll(peers, port) if peers else []
    if not any(r.get("hostname") == mine for r in reports):
        # Either no peer service is configured, or this instance did not answer
        # its own address. Either way the console must still see it.
        reports.append({"address": "self", "ok": True, **instance_report()})

    for report in reports:
        report["self"] = report.get("hostname") == mine

    reports.sort(key=lambda r: (not r["ok"], r.get("hostname") or r["address"]))

    slots = assign_slots([r["hostname"] for r in reports if r.get("hostname")])
    for report in reports:
        report["slot"] = slots.get(report.get("hostname"))

    return jsonify({
        "discovered": len(reports),
        "reachable": sum(1 for r in reports if r["ok"]),
        "source": f"dns:{os.getenv('PEER_SERVICE')}" if os.getenv("PEER_SERVICE") else "self",
        "instances": reports,
    })


MAX_SLEEP_SECONDS = 30

DEFAULT_READINESS_MARKER = os.getenv("READINESS_MARKER", "/tmp/shutdown")


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
@bp.get("/health/live")
def live():
    """Liveness: is this process wedged?

    A failure here means "restart me". It deliberately touches nothing else --
    checking a dependency from a liveness probe turns a Redis outage into a
    restart loop across every replica at once.

    /health is kept as an alias: the container HEALTHCHECK uses it.
    """
    return jsonify({"status": "ok"})


@bp.get("/health/ready")
def ready():
    """Readiness: should this pod be sent traffic right now?

    A failure here means "take me out of the load balancer, but leave me
    running" -- a different instruction from liveness, and the reason the two
    are separate endpoints.

    Note what is *not* checked: Redis. The app degrades gracefully without it,
    so a Redis outage failing readiness would empty the Service of every
    healthy replica simultaneously, converting a lost feature into a lost site.

    What does fail it is shutdown. A preStop hook drops the marker file and
    then waits, so Kubernetes removes this pod from the endpoint list before
    the server stops accepting connections. Without that gap the endpoints
    still name a pod that has already closed its listener, and a handful of
    requests fail on every single deploy.
    """
    marker = current_app.config.get("READINESS_MARKER", DEFAULT_READINESS_MARKER)
    if os.path.exists(marker):
        return jsonify({"status": "shutting down"}), 503
    return jsonify({"status": "ready"})
