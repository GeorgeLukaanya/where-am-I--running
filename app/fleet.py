"""Finding the other instances.

A single instance knows only itself, which is why /metrics fetched through a
load balancer is meaningless -- you get whichever replica answered. A console
needs the whole fleet, so each instance resolves the service name it was given
and asks every address behind it to describe itself.

That is the same mechanism a metrics collector uses for service discovery: the
container DNS returns one record per replica, so scaling up or down changes the
answer with no configuration anywhere.
"""

import json
import os
import socket
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DEFAULT_TIMEOUT = 1.0
MAX_PARALLEL = 8


def discover(service: str | None = None, port: int | None = None, resolver=None) -> list[str]:
    """Every address currently behind ``service``.

    Returns an empty list when no peer service is configured -- a lone
    container is a fleet of one, not an error.
    """
    service = os.getenv("PEER_SERVICE", "") if service is None else service
    port = int(os.getenv("PEER_PORT", "8000")) if port is None else port
    if not service:
        return []

    resolver = resolver or socket.getaddrinfo
    try:
        records = resolver(service, port, proto=socket.IPPROTO_TCP)
    except OSError:
        # The name may not resolve while the stack is starting, or ever. The
        # console degrades to this instance rather than erroring.
        return []

    return sorted({record[4][0] for record in records})


def fetch_instance(address: str, port: int, timeout: float) -> dict:
    url = f"http://{address}:{port}/api/instance"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def poll(addresses: list[str], port: int, timeout: float = DEFAULT_TIMEOUT, fetch=None) -> list[dict]:
    """Ask every address to describe itself, in parallel.

    One unreachable peer must not hold up the rest, so each request carries its
    own short timeout and failures are reported rather than raised: an instance
    that has stopped answering is exactly what a console needs to show.
    """
    fetch = fetch or fetch_instance

    def describe(address: str) -> dict:
        try:
            return {"address": address, "ok": True, **fetch(address, port, timeout)}
        except Exception as error:  # noqa: BLE001 - any failure is "not reachable"
            return {"address": address, "ok": False, "error": type(error).__name__}

    if not addresses:
        return []

    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(addresses))) as pool:
        return list(pool.map(describe, addresses))
