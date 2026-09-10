"""gunicorn settings, kept in a file so the shutdown behaviour is explicit."""

import os

bind = f"0.0.0.0:{os.getenv('PORT', '8000')}"
workers = 2
accesslog = "-"  # stdout: container logs are a stream, not a file on disk
errorlog = "-"

# How long a worker may take to finish the requests it is already serving after
# SIGTERM arrives. Docker sends SIGTERM on `docker stop` and then waits 10s
# before SIGKILL, so keep this below any `--time-limit` you set there.
graceful_timeout = 30

# Long enough that /api/slow can be used to demonstrate draining.
timeout = 60


def on_starting(server):
    server.log.info("starting: pid %s", os.getpid())


def worker_int(worker):
    worker.log.info("worker %s draining before exit", worker.pid)


def on_exit(server):
    server.log.info("shutdown complete")
