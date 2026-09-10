"""Tests for the "Where am I running?" app."""

import socket
import time

import pytest

from app import create_app
from app.fleet import discover, poll
from app.identity import PALETTE_SIZE, accent_slot, assign_slots
from app.limits import read_limits
from app.store import InMemoryCounter, RedisCounter, make_counter
from app.telemetry import read_counters


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


# --- metrics -----------------------------------------------------------------


def scrape(client) -> str:
    response = client.get("/metrics")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_metrics_endpoint_reports_request_timings(client):
    client.get("/api/info")

    body = scrape(client)

    assert "flask_http_request_duration_seconds" in body
    assert "main.api_info" in body


def test_metrics_carry_build_metadata(client):
    body = scrape(client)

    # A labelled gauge fixed at 1 -- the conventional way to publish build
    # metadata, since Prometheus has no string values. It lets a dashboard
    # display the running commit and an alert notice a rollback.
    assert 'app_build_info{' in body
    assert 'git_sha="unknown"' in body


def test_metrics_expose_counter_health_and_visits(client):
    client.get("/api/info")

    body = scrape(client)

    assert "app_counter_available 1.0" in body
    assert "app_visits" in body


def test_counter_availability_drops_when_the_backend_fails(client):
    class BrokenCounter:
        backend = "broken"

        def increment(self):
            return None

    client.application.config["COUNTER"] = BrokenCounter()
    client.get("/api/info")

    assert "app_counter_available 0.0" in scrape(client)


def test_health_checks_are_kept_out_of_request_metrics(client):
    client.get("/health")
    client.get("/health/live")
    client.get("/health/ready")

    # Probes run every few seconds per pod, forever. Counting them would swamp
    # the request-rate graph with traffic nobody sent.
    body = scrape(client)
    assert "main.live" not in body
    assert "main.ready" not in body


# --- liveness and readiness --------------------------------------------------
#
# Two different questions, and conflating them is how a dependency outage turns
# into a total one.
#
#   liveness:  is this process wedged? if so, restart it.
#   readiness: should this pod receive traffic right now? if not, take it out of
#              the load balancer -- but leave it running.


def test_liveness_is_always_ok(client):
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_health_is_an_alias_for_liveness(client):
    # The container HEALTHCHECK and older callers still use /health.
    assert client.get("/health").status_code == 200


def test_readiness_is_ok_while_serving(client):
    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ready"}


def test_readiness_survives_a_broken_dependency(client):
    class BrokenCounter:
        backend = "redis (unavailable)"

        def increment(self):
            return None

    client.application.config["COUNTER"] = BrokenCounter()

    # Redis being down degrades a feature; the pod can still serve. Failing
    # readiness here would empty the load balancer of every healthy replica at
    # once -- one broken dependency becoming a total outage.
    assert client.get("/health/ready").status_code == 200


def test_readiness_fails_once_shutdown_has_begun(client, tmp_path):
    marker = tmp_path / "shutdown"
    client.application.config["READINESS_MARKER"] = str(marker)
    marker.touch()

    response = client.get("/health/ready")

    # A preStop hook drops this marker and then waits, so the pod is pulled out
    # of the Service *before* it stops accepting connections. Without that gap
    # the endpoint list still names a pod that has already closed its listener,
    # and those requests fail during every deploy.
    assert response.status_code == 503
    assert response.get_json() == {"status": "shutting down"}


# --- instance identity colour ------------------------------------------------


def test_accent_slot_is_stable_for_a_hostname():
    assert accent_slot("3731a4862174") == accent_slot("3731a4862174")


def test_accent_slot_is_always_inside_the_palette():
    for host in ("3731a4862174", "feacdc02a322", "localhost", "", "a"):
        assert 0 <= accent_slot(host) < PALETTE_SIZE


def test_every_instance_in_a_fleet_gets_a_distinct_colour():
    fleet = ["3731a4862174", "feacdc02a322", "42da6c39007b", "c51650a870c2", "b140b9987407"]

    slots = assign_slots(fleet)

    # Two identically coloured lines would be unreadable, so collisions must be
    # resolved rather than tolerated.
    assert len(set(slots.values())) == len(fleet)


def test_slot_assignment_does_not_depend_on_arrival_order():
    fleet = ["aaa", "bbb", "ccc", "ddd"]

    assert assign_slots(fleet) == assign_slots(reversed(fleet))


def test_a_fleet_larger_than_the_palette_still_returns_a_slot_for_each():
    fleet = [f"instance{n:02d}" for n in range(12)]

    slots = assign_slots(fleet)

    assert len(slots) == 12
    assert all(0 <= slot < PALETTE_SIZE for slot in slots.values())


# --- peer discovery ----------------------------------------------------------
#
# discover() and poll() take their resolver and fetcher as arguments so these
# tests need neither DNS nor a network.


def fake_resolver(records):
    return lambda host, port, **kw: [(None, None, None, "", (ip, port)) for ip in records]


def test_no_peer_service_means_a_fleet_of_one():
    assert discover(service="", port=8000) == []


def test_discovery_returns_every_address_behind_the_name():
    resolve = fake_resolver(["10.0.0.3", "10.0.0.1", "10.0.0.2"])

    # Sorted, so the fleet list does not reshuffle between polls.
    assert discover("web", 8000, resolver=resolve) == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]


def test_discovery_collapses_duplicate_records():
    resolve = fake_resolver(["10.0.0.1", "10.0.0.1"])

    assert discover("web", 8000, resolver=resolve) == ["10.0.0.1"]


def test_discovery_survives_a_name_that_does_not_resolve():
    def broken(host, port, **kw):
        raise OSError("Name or service not known")

    assert discover("web", 8000, resolver=broken) == []


def test_poll_reports_reachable_and_unreachable_peers():
    def fetch(address, port, timeout):
        if address == "10.0.0.2":
            raise TimeoutError("too slow")
        return {"hostname": "abc123", "uptime_seconds": 4.0}

    results = poll(["10.0.0.1", "10.0.0.2"], 8000, fetch=fetch)

    assert results[0] == {"address": "10.0.0.1", "ok": True,
                          "hostname": "abc123", "uptime_seconds": 4.0}
    assert results[1] == {"address": "10.0.0.2", "ok": False, "error": "TimeoutError"}


# --- counters read back out of the metrics registry --------------------------


def test_counters_are_summed_out_of_the_registry(client):
    client.get("/api/info")
    client.get("/api/info")

    counters = read_counters(client.application.config["METRIC_REGISTRY"])

    assert counters["requests_total"] >= 2
    assert counters["request_seconds_count"] >= 2
    assert counters["request_seconds_sum"] > 0
    assert counters["errors_total"] == 0


# --- the fleet API -----------------------------------------------------------


def test_instance_endpoint_does_not_count_a_visit(client):
    before = client.get("/api/info").get_json()["visits"]
    client.get("/api/instance")
    client.get("/api/instance")
    after = client.get("/api/info").get_json()["visits"]

    # The console polls this endpoint constantly. If it counted, the console
    # would be measuring itself.
    assert after == before + 1


def test_instance_endpoint_reports_counters(client):
    body = client.get("/api/instance").get_json()

    assert body["hostname"]
    assert "requests_total" in body["counters"]
    assert "visits" in body


def test_fleet_endpoint_always_includes_this_instance(client):
    body = client.get("/api/fleet").get_json()

    # With no peer service configured the fleet is one: itself.
    assert body["discovered"] == 1
    assert body["reachable"] == 1
    assert body["instances"][0]["hostname"] == socket.gethostname()
    assert body["instances"][0]["self"] is True


def test_console_polling_is_kept_out_of_request_metrics(client):
    client.get("/api/fleet")
    client.get("/api/instance")

    body = client.get("/metrics").get_data(as_text=True)
    assert "main.fleet" not in body
    assert "main.instance" not in body
