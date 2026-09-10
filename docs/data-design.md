# Data Design

**System:** Where am I running?
**Scope:** every piece of data the system holds, where it lives, and how long
it survives.

---

## 1. The shape of the problem

This service is almost stateless, and the "almost" is the interesting part.
Nearly everything it reports is *derived* — read from the environment or the
kernel at the moment of the request, never stored. Exactly one piece of data
outlives a request, the visit count, and it is deliberately kept outside the
process.

That split is the whole data design, so it is worth stating plainly:

| Class | Example | Where it lives | Survives a restart? |
| --- | --- | --- | --- |
| **Configuration** | `APP_ENV`, `REDIS_URL` | Process environment | Supplied again on start |
| **Build metadata** | `GIT_SHA`, `IMAGE_TAG` | Baked into the image as `ENV` and labels | Immutable per image |
| **Derived facts** | hostname, uptime, cgroup limits | Computed per request | Not stored at all |
| **Shared state** | visit count | Redis, on a volume | Yes, if the volume survives |
| **Metrics** | request counts, latencies | Worker memory, mirrored to files | No — deliberately |
| **Logs** | access and error lines | stdout, captured by the runtime | Only where the runtime keeps them |

---

## 2. Configuration data

Configuration is read from the environment, never from a file, and every
variable has a working default. This is what allows one image to run in every
environment: nothing that varies between deployments is inside the image.

| Variable | Type | Default | Read by | Read when |
| --- | --- | --- | --- | --- |
| `APP_ENV` | string | `development` | `build_info()`, `metrics.install()` | Per request / at startup |
| `GIT_SHA` | string | `unknown` | `build_info()`, `metrics.install()` | Per request / at startup |
| `IMAGE_TAG` | string | `local` | `build_info()`, `metrics.install()` | Per request / at startup |
| `PORT` | integer | `8000` | `gunicorn.conf.py` | At startup |
| `REDIS_URL` | URL | *(unset)* | `make_counter()` | Once, at startup |
| `READINESS_MARKER` | path | `/tmp/shutdown` | `ready()` | Per request |
| `PROMETHEUS_MULTIPROC_DIR` | path | *(unset)* | `metrics.install()`, `gunicorn.conf.py` | At startup |

Two of these deserve comment.

`REDIS_URL` is read **once**, at startup, because the client owns a connection
pool that must not be rebuilt per request. Changing it therefore requires a
restart — which is correct for a containerised service, where configuration
changes arrive as a new container rather than as a signal.

`READINESS_MARKER` is checked **per request**, because its whole purpose is to
change while the process is running.

---

## 3. The runtime information payload

This is the service's public data contract, returned by `GET /api/info` and
rendered by `GET /`.

```json
{
  "hostname": "c51650a870c2",
  "environment": "production",
  "image_tag": "georgelukaanya/where-am-i-running:compose",
  "git_sha": "6bdac71",
  "python_version": "3.12.14",
  "uptime_seconds": 39.0,
  "memory_limit": "128 MB",
  "cpu_limit": "0.5 CPUs",
  "visits": 27,
  "counter_backend": "redis"
}
```

| Field | Type | Source | Notes |
| --- | --- | --- | --- |
| `hostname` | string | `socket.gethostname()` | The container ID under Docker. The field that makes replicas distinguishable. |
| `environment` | string | `APP_ENV` | |
| `image_tag` | string | `IMAGE_TAG` | `local` when not built by a pipeline |
| `git_sha` | string | `GIT_SHA` | `unknown` when not built by a pipeline |
| `python_version` | string | `platform.python_version()` | The interpreter *inside* the container |
| `uptime_seconds` | number | monotonic clock delta | Per process, not per container: a replaced worker resets it |
| `memory_limit` | string | cgroup | `"128 MB"` or `"unlimited"` |
| `cpu_limit` | string | cgroup | `"0.5 CPUs"` or `"unlimited"` |
| `visits` | integer \| null | counter backend | `null` when the backend is unreachable |
| `counter_backend` | string | counter backend | `in-memory`, `redis`, or `redis (unavailable)` |

**Design notes.**

Limits are formatted strings rather than numbers. They are for a human reading
a page, and `"unlimited"` has no numeric equivalent that is not a lie — `0` and
`-1` both invite arithmetic that produces nonsense. Machines that need the
numbers should read the metrics endpoint, which is typed.

`visits` is nullable, and null means *the count is unknown*, which is different
from zero. Reporting `0` during a Redis outage would be a fabricated value.

---

## 4. Persistent data: the visit count

The only datum that outlives a request.

| Property | Value |
| --- | --- |
| Store | Redis 7 |
| Key | `visits` |
| Type | String, used as an integer counter |
| Operation | `INCR` — atomic, so concurrent replicas cannot lose an increment |
| TTL | None; the count is intended to be permanent |
| Size | One key, a handful of bytes, forever |
| Durability | Append-only file (`--appendonly yes`) written to `/data` |
| Backing store | Named volume `redis-data` |
| Credentials | **None.** See §7. |

### 4.1 Why `INCR` and not read-modify-write

`INCR` is atomic on the server. Three replicas incrementing concurrently
produce three increments. A read, add, write sequence from the application
would lose updates under exactly the concurrency this system is built to
demonstrate.

### 4.2 Lifetime, precisely

This is the distinction the system exists to make visible:

| Event | Count survives? | Why |
| --- | --- | --- |
| A worker process is replaced | Yes | The count was never in the process |
| An app replica is killed | Yes | The count was never in that container |
| `docker compose restart` | Yes | Redis keeps its volume |
| `docker compose down` | Yes | Containers are destroyed; the **volume is not** |
| `docker compose down -v` | **No** | `-v` destroys the volume, and the data with it |
| Redis container replaced, volume kept | Yes | The AOF is replayed from the volume |
| Redis crashes between AOF writes | Possibly not | AOF fsync policy trades durability for speed |

Verified behaviour: `down` then `up` continued the count at 33; `down -v` then
`up` restarted it at 1.

### 4.3 What happens with no Redis

`make_counter()` returns an in-memory counter, which is a plain integer in the
process. It is private to each worker of each replica, so with three replicas
of two workers each, six independent counts exist and the number a caller sees
depends on who answered. That is not a bug to be tolerated but the point being
demonstrated: state kept in a process belongs to that process.

---

## 5. Metrics data

Metrics are numeric time series held in memory and scraped, not stored by the
service. Prometheus owns retention; the service owns only the current value.

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `flask_http_request_total` | counter | `method`, `status` | Requests served |
| `flask_http_request_duration_seconds` | histogram | `method`, `status`, `endpoint` | Request latency |
| `flask_http_request_exceptions_total` | counter | `method`, `status` | Unhandled exceptions |
| `app_build_info` | gauge (always 1) | `git_sha`, `image_tag`, `environment` | Build metadata |
| `app_counter_available` | gauge | — | 1 when the counter answered, 0 when it did not |
| `app_visits` | gauge | — | Current shared visit count |

### 5.1 Why build metadata is a gauge fixed at 1

Prometheus has no string type. The convention is a gauge whose value is
meaningless and whose *labels* carry the information, so a dashboard can
display the running commit and an alert can fire when it changes unexpectedly.

### 5.2 Cardinality

Cardinality is the cost model of a metrics system: every distinct label
combination is a separate stored series, permanently. The labels here are
bounded by design — HTTP methods, status codes, and a fixed set of endpoints —
so the series count is bounded too. Nothing per-request is ever a label. Adding
the hostname as a label would multiply every series by the number of replicas
that have *ever* run, which is unbounded in an autoscaled system; the scraper
attaches instance identity itself, which is the correct place for it.

`app_build_info` has one series per deployed build, which is bounded by
deployment frequency and is the point of the metric.

### 5.3 Multi-process aggregation

Under gunicorn each worker is a separate process holding its own counters, so a
naive scrape reports whichever worker answered — numbers that appear to jump
around at random.

```
PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus
   ├── counter_7.db      worker 7 mirrors its counters here
   ├── counter_8.db      worker 8 mirrors its counters here
   └── gauge_max_*.db    gauges, aggregated by the mode declared on them
```

The `/metrics` endpoint reads that directory and sums across the files, so 20
requests spread over 2 workers report as 20. Two consequences that are easy to
get wrong:

- **The directory must be empty at start**, or a previous run's files are
  counted again. `on_starting` in `gunicorn.conf.py` clears it.
- **A dead worker's files must be retired**, or its last values are summed into
  every scrape forever and the numbers drift upward. `child_exit` calls
  `mark_process_dead`.

Gauges need an aggregation mode, since summing "current value" across workers
is meaningless. All three use `max`, which for `app_visits` yields the highest
count any worker has seen — the closest available approximation to the shared
truth in Redis.

---

## 6. Log data

Access and error logs go to stdout as a stream, never to a file. A container's
filesystem is ephemeral, so a log file inside it is data waiting to be deleted;
the runtime captures the stream and owns it from there.

Current format is gunicorn's default combined log line — human-readable, not
machine-parseable. Structured JSON logging is planned and not built.

Log lines contain a client IP and a request path. No user data exists in the
system to leak into them.

---

## 7. Data security

| Concern | Status |
| --- | --- |
| Personal data | None held. |
| Authentication data | None; the service has no accounts. |
| Secrets in the image | None. |
| Secrets in the repository | None. |
| **Redis authentication** | **Absent.** Redis accepts any connection on the compose network. |
| Encryption in transit | None. All traffic is plain HTTP, including to Redis. |
| Encryption at rest | None; the volume is unencrypted. |

The last three are acceptable only while everything runs on one laptop behind a
Docker bridge network. **Redis must be given a password, and traffic must be
carried over TLS, before this is deployed anywhere reachable.** These are
tracked as work items rather than accepted risks.

---

## 8. Planned changes for Kubernetes

*(Not implemented.)*

- Configuration moves to a `ConfigMap`; `REDIS_URL` and the Redis password move
  to a `Secret`, mounted as environment variables rather than written into any
  manifest committed to the repository.
- The Redis volume becomes a `PersistentVolumeClaim` bound by a StatefulSet,
  which changes the failure modes in §4.2: a rescheduled pod re-binds the same
  claim, and the count survives the node it was running on.
- `PROMETHEUS_MULTIPROC_DIR` needs an `emptyDir` volume once the root
  filesystem is made read-only, since `/tmp` will no longer be writable.
- The readiness marker also lives on that writable volume, written by a preStop
  hook.
