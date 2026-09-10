"""Reading this instance's own counters back out of the metrics registry.

The numbers are already being collected for /metrics; the console needs them as
JSON rather than exposition text. Reading the registry directly avoids the app
making an HTTP request to itself.

Counters are reported as running totals, never as rates. A rate depends on two
samples and the time between them, and only the caller knows when it last
looked -- computing it here would mean inventing a window.
"""

import os

from prometheus_client import CollectorRegistry, multiprocess

REQUESTS = "flask_http_request_total"
DURATION_SUM = "flask_http_request_duration_seconds_sum"
DURATION_COUNT = "flask_http_request_duration_seconds_count"


def current_registry(app):
    """The registry holding this instance's totals.

    Under gunicorn each worker counts separately, so the totals only exist once
    the per-worker files are collected together.
    """
    if os.getenv("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return registry
    return app.config["METRIC_REGISTRY"]


def read_counters(registry) -> dict:
    totals = {
        "requests_total": 0.0,
        "errors_total": 0.0,
        "request_seconds_sum": 0.0,
        "request_seconds_count": 0.0,
    }

    for family in registry.collect():
        for sample in family.samples:
            if sample.name == REQUESTS:
                totals["requests_total"] += sample.value
                if str(sample.labels.get("status", "")).startswith("5"):
                    totals["errors_total"] += sample.value
            elif sample.name == DURATION_SUM:
                totals["request_seconds_sum"] += sample.value
            elif sample.name == DURATION_COUNT:
                totals["request_seconds_count"] += sample.value

    return totals
