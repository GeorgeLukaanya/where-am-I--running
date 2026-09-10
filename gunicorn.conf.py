"""gunicorn settings, kept in a file so the shutdown behaviour is explicit."""

import os
import shutil
from pathlib import Path

bind = f"0.0.0.0:{os.getenv('PORT', '8000')}"
workers = 2
accesslog = "-"  # stdout: container logs are a stream, not a file on disk
errorlog = "-"

# How long a worker may take to finish the requests it is already serving after
# SIGTERM arrives. Docker sends SIGTERM on `docker stop` and then waits 10s
# before SIGKILL, so a drain longer than that needs `docker stop -t`.
graceful_timeout = 30

# Long enough that /api/slow can be used to demonstrate draining.
timeout = 60

METRICS_DIR = os.getenv("PROMETHEUS_MULTIPROC_DIR")


def on_starting(server):
    """Start from an empty metrics directory.

    Each worker mirrors its counters into files here so /metrics can sum across
    them. Files left by a previous run would be counted again, so the totals
    would start out wrong after every restart.
    """
    if METRICS_DIR:
        shutil.rmtree(METRICS_DIR, ignore_errors=True)
        Path(METRICS_DIR).mkdir(parents=True, exist_ok=True)
    server.log.info("starting: pid %s", os.getpid())


def child_exit(server, worker):
    """Retire a dead worker's gauges.

    Without this its last values keep being summed into every scrape forever,
    and the numbers drift upward each time a worker is replaced.
    """
    if METRICS_DIR:
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(worker.pid)


def worker_int(worker):
    worker.log.info("worker %s draining before exit", worker.pid)


def on_exit(server):
    server.log.info("shutdown complete")
