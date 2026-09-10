"""Prometheus instrumentation.

Metrics are pull-based: nothing is sent anywhere. The app publishes its current
numbers at /metrics and Prometheus comes and reads them on a schedule. That is
why scaling to three replicas needs no configuration change on the app side --
Prometheus discovers each one and scrapes it.
"""

import os

from flask import current_app
from prometheus_client import REGISTRY, CollectorRegistry, Gauge

EXCLUDED_PATHS = ["/health", "/metrics"]


def install(app) -> None:
    """Attach the /metrics endpoint to this app.

    Under gunicorn each worker is a separate process with its own counters, so
    a naive /metrics would return whichever worker happened to serve the scrape
    and the numbers would appear to jump about at random. In multiprocess mode
    the workers write to a shared directory and the endpoint sums across them.
    """
    # The healthcheck fires every 30 seconds forever. Counting it would swamp
    # the request-rate graph with traffic nobody sent.
    options = {"group_by": "endpoint", "excluded_paths": EXCLUDED_PATHS}

    if os.getenv("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_flask_exporter.multiprocess import (
            GunicornInternalPrometheusMetrics as Metrics,
        )

        # Two registries, deliberately. The gauges go in the default one, which
        # is what gets mirrored into the shared directory. The endpoint gets its
        # own, holding only a collector that reads that directory back -- pass
        # it the default registry instead and the collector claims the names of
        # metrics an earlier worker already wrote, so the next worker to boot
        # collides with itself and gunicorn never comes up.
        Metrics(app, **options)
        registry = REGISTRY
    else:
        from prometheus_flask_exporter import PrometheusMetrics as Metrics

        # A registry per app. Sharing the default one means only the first app
        # instrumented in a process is ever exported, which is invisible in
        # production (one app per process) and quietly wrong under test.
        registry = CollectorRegistry()
        Metrics(app, registry=registry, **options)

    app.config["GAUGES"] = _build_gauges(registry)
    app.config["GAUGES"]["build_info"].labels(
        git_sha=os.getenv("GIT_SHA", "unknown"),
        image_tag=os.getenv("IMAGE_TAG", "local"),
        environment=os.getenv("APP_ENV", "development"),
    ).set(1)


def _build_gauges(registry) -> dict:
    return {
        # Prometheus has no string values, so build metadata is published the
        # conventional way: a gauge pinned at 1 whose labels carry the
        # information. A dashboard can then display the running commit, and an
        # alert can notice a deployment or a rollback.
        "build_info": Gauge(
            "app_build_info",
            "Build metadata; the value is always 1",
            ["git_sha", "image_tag", "environment"],
            multiprocess_mode="max",
            registry=registry,
        ),
        "counter_available": Gauge(
            "app_counter_available",
            "1 when the visit counter backend answered, 0 when it did not",
            multiprocess_mode="max",
            registry=registry,
        ),
        "visits": Gauge(
            "app_visits",
            "Visits recorded by the shared counter",
            multiprocess_mode="max",
            registry=registry,
        ),
    }


def record_visit(count: int | None) -> None:
    """Publish the outcome of a counter increment."""
    gauges = current_app.config["GAUGES"]
    gauges["counter_available"].set(0 if count is None else 1)
    if count is not None:
        gauges["visits"].set(count)
