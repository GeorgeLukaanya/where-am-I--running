# Software Requirements Specification

**System:** Where am I running?
**Version:** 1.0 — describes the system as built at commit `95f0597`
**Status:** Current. Requirements for the planned Kubernetes deployment are
marked *(planned)* and are not yet implemented.

---

## 1. Introduction

### 1.1 Purpose

This document specifies what the *Where am I running?* service must do and how
well it must do it. It is the reference for what counts as correct behaviour,
and the other design documents refer back to the requirement identifiers
defined here.

### 1.2 Scope

The system is a small HTTP service that answers one question: **which instance
am I talking to, and under what conditions is it running?**

It reports the identity of the process serving a request (hostname, which
inside a container is the container ID), the provenance of the code (the commit
and image tag it was built from), the environment it was configured for, the
resource limits the kernel is enforcing on it, and how long it has been up. It
also counts visits, in a store that is shared between instances when one is
configured.

That makes it useful for two things:

1. **Observing infrastructure behaviour.** Because every instance reports its
   own identity and configuration, behaviour that is normally invisible becomes
   directly observable: load balancing across replicas, the difference between
   per-instance and shared state, what a volume does and does not preserve,
   what a cgroup limit actually enforces, and whether a shutdown drains or
   drops requests.
2. **Serving as a reference deployment.** The service is packaged, configured,
   probed, instrumented, tested and released the way a production service
   should be, so the surrounding machinery — image, pipeline, probes, metrics —
   is reusable as a template.

**Out of scope:** authentication, authorisation, multi-tenancy, user accounts,
durable business data, and any workload beyond reporting its own state.

### 1.3 Definitions and abbreviations

| Term | Meaning |
| --- | --- |
| **Instance** | One running process of the service. In a container, one container. |
| **Replica** | One of several interchangeable instances behind a load balancer. |
| **cgroup** | Linux control group; the kernel mechanism that caps a process's CPU and memory. |
| **Liveness** | Whether a process is wedged and should be restarted. |
| **Readiness** | Whether an instance should currently receive traffic. |
| **Drain** | Finishing in-flight requests before exiting. |
| **SBOM** | Software Bill of Materials; a machine-readable inventory of what is inside an image. |
| **Exposition format** | The Prometheus text format served at `/metrics`. |

### 1.4 References

- `docs/architecture.md` — structure, deployment topologies, and the reasoning behind the major decisions
- `docs/system-design.md` — components, interfaces, control flow and failure handling
- `docs/data-design.md` — every piece of data the system holds, and how long it lasts
- The Twelve-Factor App, factor III (config in the environment)

### 1.5 Overview

Section 2 describes the system in context. Section 3 states the requirements.
Section 4 maps each requirement to the code and test that satisfy it.

---

## 2. Overall description

### 2.1 Product perspective

The service is self-contained. It has one optional external dependency (a Redis
instance) and degrades to a reduced but correct mode without it. It holds no
durable data of its own; everything it reports is either read from its
environment, read from the kernel, or computed at the moment of the request.

It is designed to run as several interchangeable replicas behind a load
balancer, and to be started and stopped frequently.

### 2.2 Product functions

- Report the identity, provenance, configuration and resource limits of the
  instance serving the request, as HTML and as JSON.
- Count visits, in a shared store when one is configured.
- Report liveness and readiness separately, for use by an orchestrator.
- Publish operational metrics in Prometheus exposition format.
- Delay a response on request, so that shutdown draining can be observed.

### 2.3 User characteristics

| User | Interest |
| --- | --- |
| **Operator** | Which build is running where; whether an instance is healthy; what the metrics say. |
| **Engineer learning the platform** | Watching infrastructure behaviour become visible through a real service. |
| **Automated client** | Probes, scrapers, and the CI smoke test, which consume JSON and the exposition format. |

No user is authenticated, and no user-specific data exists.

### 2.4 Constraints

- **C-1** Python 3.12 on `python:3.12-slim`; served by gunicorn with multiple
  worker processes.
- **C-2** Must run as an unprivileged user, with no build toolchain and no
  package manager caches in the runtime image.
- **C-3** Must run unchanged on `linux/amd64` and `linux/arm64`.
- **C-4** Must run under both cgroup v1 and cgroup v2 hosts.
- **C-5** All configuration arrives through environment variables. No
  configuration file is read at runtime, and no value that varies between
  deployments may be baked into the image.
- **C-6** The service holds no durable state. Anything that must survive an
  instance belongs in an external store.

### 2.5 Assumptions and dependencies

- **A-1** A container runtime enforces the cgroup limits reported; the service
  reads them, it does not apply them.
- **A-2** When `REDIS_URL` is set, a Redis-compatible server is reachable at
  that address. The service must not assume it *stays* reachable.
- **A-3** The orchestrator sends `SIGTERM` and allows a grace period before
  `SIGKILL`.
- **A-4** The build pipeline supplies the commit and image tag as build
  arguments; without them the service reports honest placeholders rather than
  guessing.

---

## 3. Specific requirements

### 3.1 External interface requirements

#### 3.1.1 HTTP interface

| Method | Path | Response |
| --- | --- | --- |
| GET | `/` | `200` HTML page of the runtime facts |
| GET | `/api/info` | `200` JSON of the same facts |
| GET | `/health` | `200` — alias of `/health/live` |
| GET | `/health/live` | `200` `{"status":"ok"}` |
| GET | `/health/ready` | `200` `{"status":"ready"}` or `503` `{"status":"shutting down"}` |
| GET | `/metrics` | `200` Prometheus exposition format |
| GET | `/api/slow?seconds=N` | `200` after waiting, or `400` if `N` is not a number |

The JSON payload is specified in `docs/data-design.md` §3.

#### 3.1.2 Configuration interface

All configuration is read from the environment. Every variable has a default,
and the service starts with none of them set.

| Variable | Default | Effect |
| --- | --- | --- |
| `APP_ENV` | `development` | Reported as the environment |
| `GIT_SHA` | `unknown` | Reported as the source commit |
| `IMAGE_TAG` | `local` | Reported as the image |
| `PORT` | `8000` | Listening port |
| `REDIS_URL` | *(unset)* | When set, visits are counted in Redis |
| `READINESS_MARKER` | `/tmp/shutdown` | Path whose existence fails readiness |
| `PROMETHEUS_MULTIPROC_DIR` | *(unset)* | Directory workers share metric state through |

### 3.2 Functional requirements

**Runtime reporting**

- **FR-1** The service shall report the hostname of the process serving the
  request.
- **FR-2** The service shall report the commit and image tag the running code
  was built from, and shall report a documented placeholder — not a guess —
  when the build did not supply them.
- **FR-3** The service shall report the environment name it was configured
  with.
- **FR-4** The service shall report the memory and CPU limits its cgroup
  enforces, supporting both cgroup v2 and cgroup v1, and shall report
  `unlimited` when no limit is set or the values cannot be read.
- **FR-5** The service shall report the elapsed time since the process started.
- **FR-6** The service shall offer the same facts as an HTML page and as JSON.

**Visit counting**

- **FR-7** The service shall count visits to `/` and `/api/info`, and report
  the running count and which backend holds it.
- **FR-8** When `REDIS_URL` is set the count shall be held in Redis, shared by
  all instances; otherwise it shall be held in process memory, private to each
  instance.
- **FR-9** When the configured counter backend cannot be reached, the service
  shall continue to serve every route successfully, report the backend as
  unavailable, and report no count.

**Health**

- **FR-10** The service shall expose a liveness endpoint that reports the
  process is running and does not query any external dependency.
- **FR-11** The service shall expose a readiness endpoint, separate from
  liveness, reporting whether the instance should receive traffic.
- **FR-12** Readiness shall not fail because an external dependency is
  unavailable.
- **FR-13** Readiness shall fail with `503` when the shutdown marker file
  exists, so that an instance can be removed from a load balancer before it
  stops accepting connections.

**Observability**

- **FR-14** The service shall publish request counts, request durations and
  error counts in Prometheus exposition format.
- **FR-15** The service shall publish its build metadata, the visit count, and
  whether the counter backend is answering, as metrics.
- **FR-16** Health and metrics endpoints shall be excluded from request
  metrics.
- **FR-17** Metrics shall aggregate across all worker processes of an instance,
  not report a single worker's view.

**Lifecycle**

- **FR-18** On `SIGTERM` the service shall stop accepting new connections,
  complete in-flight requests, and then exit.
- **FR-19** The service shall provide an endpoint that delays its response by a
  caller-specified duration, bounded by a fixed maximum, so that draining can
  be observed. Invalid input shall be rejected with `400`.

### 3.3 Non-functional requirements

**Availability**

- **NFR-1** Loss of the counter backend shall not cause any request to fail.
  Every route shall keep returning its normal status code.
- **NFR-2** No probe shall report failure on account of a dependency outage,
  so that an outage of one dependency cannot remove every healthy replica from
  service simultaneously.
- **NFR-3** A shutdown shall not drop an in-flight request, given a grace
  period at least as long as the request.

**Performance**

- **NFR-4** A request to `/api/info` shall be served in under 50 ms at the
  99th percentile when the counter backend is responsive.
- **NFR-5** An instance shall be ready to serve within 5 seconds of start.
- **NFR-6** A failing counter backend shall not delay a response by more than
  1 second, enforced by a connection timeout rather than by hope.

**Security**

- **NFR-7** The runtime image shall contain no build toolchain, no package
  manager cache and no application source beyond what is executed.
- **NFR-8** The service shall run as an unprivileged user.
- **NFR-9** The image shall be scanned on every build, and a fixable
  HIGH or CRITICAL vulnerability shall fail the build.
- **NFR-10** Every published image shall carry an SBOM and build provenance
  identifying the commit and workflow that produced it.
- **NFR-11** No credential shall be present in the image, in the repository, or
  in any configuration committed to the repository.

**Portability**

- **NFR-12** One published tag shall serve both `linux/amd64` and
  `linux/arm64`.
- **NFR-13** The service shall behave identically on cgroup v1 and cgroup v2
  hosts, differing only in the values it reports.
- **NFR-14** A single image shall be deployable to any environment with no
  rebuild, differing only by environment variables.

**Maintainability**

- **NFR-15** Every functional requirement shall be covered by an automated
  test that does not require a live external dependency.
- **NFR-16** The stack itself — image build, load balancing and shared state —
  shall be verified by an automated integration test, not only by unit tests.

---

## 4. Traceability

| Requirement | Implemented in | Verified by |
| --- | --- | --- |
| FR-1, FR-3, FR-5, FR-6 | `app/main.py` `build_info()` | `test_api_info_reports_the_runtime_environment`, `test_index_page_shows_the_hostname` |
| FR-2 | `app/main.py`, `Dockerfile` build args | `test_api_info_reads_build_metadata_from_the_environment` |
| FR-4, NFR-13 | `app/limits.py` | `test_reads_cgroup_v2_limits`, `test_falls_back_to_cgroup_v1_layout`, `test_cgroup_v1_sentinel_value_means_unlimited` |
| FR-7, FR-8 | `app/store.py` | `test_in_memory_counter_counts_within_this_process`, `test_make_counter_uses_redis_when_configured_without_connecting` |
| FR-9, NFR-1, NFR-6 | `app/store.py` `RedisCounter.increment` | `test_redis_counter_degrades_instead_of_failing`, `test_health_stays_ok_when_the_counter_is_broken` |
| FR-10, FR-11 | `app/main.py` `live()`, `ready()` | `test_liveness_is_always_ok`, `test_readiness_is_ok_while_serving` |
| FR-12, NFR-2 | `app/main.py` `ready()` | `test_readiness_survives_a_broken_dependency` |
| FR-13, NFR-3 | `app/main.py` `ready()`, preStop hook *(planned)* | `test_readiness_fails_once_shutdown_has_begun` |
| FR-14, FR-15 | `app/metrics.py` | `test_metrics_endpoint_reports_request_timings`, `test_metrics_carry_build_metadata` |
| FR-16 | `app/metrics.py` `EXCLUDED_PATHS` | `test_health_checks_are_kept_out_of_request_metrics` |
| FR-17 | `app/metrics.py`, `gunicorn.conf.py` | Manual: 20 requests across 2 workers sum to 20 |
| FR-18 | `Dockerfile` `CMD` (`exec`), `gunicorn.conf.py` | Manual: in-flight request completes during `docker stop` |
| FR-19 | `app/main.py` `slow()` | `test_slow_endpoint_waits_before_answering`, `test_slow_endpoint_caps_the_wait`, `test_slow_endpoint_rejects_nonsense` |
| NFR-7, NFR-8 | `Dockerfile` multi-stage, `USER appuser` | Manual: `docker exec … id` |
| NFR-9, NFR-10, NFR-12 | `.github/workflows/docker-build.yml` | The pipeline itself |
| NFR-16 | `.github/workflows/docker-build.yml` `smoke` job | The pipeline itself |

**Known gaps.** FR-17 and FR-18 are verified manually rather than by an
automated test. NFR-4 and NFR-5 are stated but not measured — no load test
exists. NFR-11 currently holds only because the system has no credentials to
leak: Redis runs without a password, which must change before any deployment
that is not a laptop.
