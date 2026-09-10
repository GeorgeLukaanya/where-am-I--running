"""The visit counter, in two flavours.

Which one is used depends only on whether ``REDIS_URL`` is set, and that is the
whole point: with no Redis, every replica counts on its own and the number
jumps about as requests land on different containers; with Redis, the replicas
share one number. Same image either way.
"""

import os

import redis
from redis.exceptions import RedisError

VISITS_KEY = "visits"


class InMemoryCounter:
    """Counts inside this process, and dies with the container.

    Useful precisely because it is wrong for anything real: it demonstrates
    that a container's filesystem and memory are ephemeral.
    """

    backend = "in-memory"

    def __init__(self) -> None:
        self._count = 0

    def increment(self) -> int:
        self._count += 1
        return self._count

    def read(self) -> int:
        return self._count


class RedisCounter:
    """Counts in a Redis service shared by every replica.

    The client is created without touching the network — redis-py connects
    lazily, on the first command — so constructing this never fails, and a
    Redis that is down degrades the feature rather than the application.
    """

    def __init__(self, url: str, key: str = VISITS_KEY) -> None:
        self._client = redis.Redis.from_url(
            url, socket_connect_timeout=1, socket_timeout=1
        )
        self._key = key
        self._available = True

    @property
    def backend(self) -> str:
        return "redis" if self._available else "redis (unavailable)"

    def increment(self) -> int | None:
        try:
            count = self._client.incr(self._key)
        except RedisError:
            self._available = False
            return None
        self._available = True
        return count

    def read(self) -> int | None:
        """The current count, without recording a visit.

        The console polls constantly; if looking at the number changed it, the
        console would be measuring itself.
        """
        try:
            value = self._client.get(self._key)
        except RedisError:
            self._available = False
            return None
        self._available = True
        return int(value or 0)


def make_counter(url: str | None = None):
    """Pick a backend from the environment.

    The address is a service name (``redis://redis:6379/0``), not an IP:
    Docker's embedded DNS resolves it on the compose network. That is service
    discovery, and it is why the same image works unchanged in any environment
    that provides a host called ``redis``.
    """
    url = os.getenv("REDIS_URL", "") if url is None else url
    return RedisCounter(url) if url else InMemoryCounter()
