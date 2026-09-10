"""Tests for the "Where am I running?" app."""

import socket

import pytest

from app import create_app
from app.store import InMemoryCounter, RedisCounter, make_counter


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_health_returns_ok(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_api_info_reports_the_runtime_environment(client):
    response = client.get("/api/info")

    assert response.status_code == 200
    info = response.get_json()
    assert info["hostname"] == socket.gethostname()
    assert info["environment"] == "development"
    assert info["image_tag"] == "local"
    assert info["git_sha"] == "unknown"
    assert info["python_version"].startswith("3.")
    assert info["uptime_seconds"] >= 0


def test_api_info_reads_build_metadata_from_the_environment(client, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("IMAGE_TAG", "georgelukaanya/where-am-i-running:v1")
    monkeypatch.setenv("GIT_SHA", "abc1234")

    info = client.get("/api/info").get_json()

    assert info["environment"] == "production"
    assert info["image_tag"] == "georgelukaanya/where-am-i-running:v1"
    assert info["git_sha"] == "abc1234"


def test_index_page_shows_the_hostname(client):
    response = client.get("/")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert socket.gethostname() in body
    assert "Where am I running?" in body


# --- the visit counter -------------------------------------------------------


def test_in_memory_counter_counts_within_this_process():
    counter = InMemoryCounter()

    assert [counter.increment() for _ in range(3)] == [1, 2, 3]
    assert counter.backend == "in-memory"


def test_make_counter_falls_back_to_memory_without_a_redis_url(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)

    assert isinstance(make_counter(), InMemoryCounter)


def test_make_counter_uses_redis_when_configured_without_connecting(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")

    # Nothing is listening on that name here: constructing must not raise,
    # because the client connects lazily, on first use.
    assert isinstance(make_counter(), RedisCounter)


def test_redis_counter_degrades_instead_of_failing():
    counter = RedisCounter("redis://127.0.0.1:1/0")

    assert counter.increment() is None
    assert counter.backend == "redis (unavailable)"


def test_api_info_reports_the_visit_count(client):
    first = client.get("/api/info").get_json()
    second = client.get("/api/info").get_json()

    assert first["counter_backend"] == "in-memory"
    assert second["visits"] == first["visits"] + 1


def test_health_stays_ok_when_the_counter_is_broken(client):
    class BrokenCounter:
        backend = "broken"

        def increment(self):
            raise RuntimeError("counter is down")

    client.application.config["COUNTER"] = BrokenCounter()

    # The application itself is healthy; only a feature is degraded. A health
    # check that fails here would take a serving container out of rotation for
    # no reason.
    assert client.get("/health").status_code == 200
