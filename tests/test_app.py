"""Tests for the "Where am I running?" app."""

import socket
import time

import pytest

from app import create_app
from app.limits import read_limits
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


# --- cgroup limits -----------------------------------------------------------
#
# read_limits() takes the cgroup root as an argument precisely so these tests
# can hand it a directory of fixture files instead of mocking open().


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_reads_cgroup_v2_limits(tmp_path):
    write(tmp_path / "memory.max", "134217728\n")
    write(tmp_path / "cpu.max", "50000 100000\n")

    assert read_limits(tmp_path) == {"memory_limit": "128 MB", "cpu_limit": "0.5 CPUs"}


def test_reads_cgroup_v2_without_limits(tmp_path):
    write(tmp_path / "memory.max", "max\n")
    write(tmp_path / "cpu.max", "max 100000\n")

    assert read_limits(tmp_path) == {
        "memory_limit": "unlimited",
        "cpu_limit": "unlimited",
    }


def test_falls_back_to_cgroup_v1_layout(tmp_path):
    write(tmp_path / "memory" / "memory.limit_in_bytes", "268435456\n")
    write(tmp_path / "cpu" / "cpu.cfs_quota_us", "200000\n")
    write(tmp_path / "cpu" / "cpu.cfs_period_us", "100000\n")

    assert read_limits(tmp_path) == {"memory_limit": "256 MB", "cpu_limit": "2 CPUs"}


def test_cgroup_v1_sentinel_value_means_unlimited(tmp_path):
    # cgroup v1 has no "max": an unlimited memory cgroup reports a number near
    # the top of a signed 64-bit integer instead.
    write(tmp_path / "memory" / "memory.limit_in_bytes", "9223372036854771712\n")
    write(tmp_path / "cpu" / "cpu.cfs_quota_us", "-1\n")
    write(tmp_path / "cpu" / "cpu.cfs_period_us", "100000\n")

    assert read_limits(tmp_path) == {
        "memory_limit": "unlimited",
        "cpu_limit": "unlimited",
    }


def test_missing_cgroup_files_report_unlimited(tmp_path):
    assert read_limits(tmp_path) == {
        "memory_limit": "unlimited",
        "cpu_limit": "unlimited",
    }


def test_api_info_reports_the_limits(client):
    info = client.get("/api/info").get_json()

    assert "memory_limit" in info
    assert "cpu_limit" in info


# --- graceful shutdown -------------------------------------------------------


def test_slow_endpoint_waits_before_answering(client):
    started = time.monotonic()
    response = client.get("/api/slow?seconds=0.2")

    assert response.status_code == 200
    assert response.get_json()["slept_seconds"] == 0.2
    assert time.monotonic() - started >= 0.2


def test_slow_endpoint_caps_the_wait(client, monkeypatch):
    # Nobody gets to hold a worker open for an hour. Assert on what the route
    # asks to sleep for rather than waiting it out -- a test suite that takes
    # 30 seconds to prove a cap is a test suite nobody runs.
    slept = []
    monkeypatch.setattr("time.sleep", slept.append)

    body = client.get("/api/slow?seconds=9999").get_json()

    assert body["slept_seconds"] == 30
    assert slept == [30]


def test_slow_endpoint_rejects_nonsense(client):
    assert client.get("/api/slow?seconds=abc").status_code == 400
