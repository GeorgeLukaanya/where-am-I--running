# Where am I running?

A small Flask app that reports the environment it is running in: the hostname
(inside a container, that is the container ID), the image tag, the commit the
image was built from, the resource limits it is confined to, and how long it
has been up.

Every value that varies between builds and deployments arrives through an
environment variable, so a single image runs anywhere without a rebuild. That
makes it a useful thing to point at when you want to *show* what containers do
rather than describe it: scale it up and watch requests land on different
hostnames; restart it and watch which state survives; constrain it and watch it
report its own ceiling.

| Route | Purpose |
| --- | --- |
| `GET /` | HTML card showing the runtime details |
| `GET /api/info` | The same details as JSON |
| `GET /health/live` | Liveness — is the process wedged? A failure means restart me |
| `GET /health/ready` | Readiness — should I be sent traffic? A failure means take me out of the load balancer |
| `GET /health` | Alias for `/health/live`, used by the container `HEALTHCHECK` |
| `GET /metrics` | Prometheus exposition format |
| `GET /api/slow?seconds=N` | Holds the request open, for observing shutdown draining |

## Quick start

```bash
docker build -t georgelukaanya/where-am-i-running:latest \
  --build-arg GIT_SHA=$(git rev-parse --short HEAD) \
  --build-arg IMAGE_TAG=georgelukaanya/where-am-i-running:latest .

docker run -d --name wair -p 8081:8000 georgelukaanya/where-am-i-running:latest

curl -s localhost:8081/health    # {"status":"ok"}
curl -s localhost:8081/api/info  # full JSON
```

Then open <http://localhost:8081>. Clean up with `docker rm -f wair`.

## The image

Multi-stage, on `python:3.12-slim`:

- the **builder** stage installs dependencies into `/install`, so pip's caches
  and build leftovers never reach the final image;
- the **runtime** stage copies that prefix in, adds the application, and runs as
  the unprivileged `appuser` (uid 10001) rather than root;
- `ARG GIT_SHA` / `ARG IMAGE_TAG` become both OCI image labels and runtime
  environment variables, which is how a pipeline stamps a build into the app;
- `HEALTHCHECK` polls `/health` so Docker itself reports the container healthy;
- gunicorn serves the app — the Flask development server is not meant for
  anything but development.

Resulting image: about 136 MB.

## Running the stack

`docker-compose.yml` puts the app behind an nginx reverse proxy with Redis for
shared state, so it can be scaled to several containers sharing one address.

```bash
docker compose up -d --build --scale web=3
docker compose ps          # 3 web replicas + proxy + redis
```

The stack is served on <http://localhost:8085> (the proxy). The replicas
publish no host port — they are reachable only inside the Docker network.

### Requests are spread across replicas

```bash
for i in $(seq 1 6); do curl -s localhost:8085/api/info | grep -o '"hostname":"[^"]*"'; done
```

More than one container ID comes back. Open the page and hold refresh to watch
the Hostname row change.

**One trap worth knowing about.** nginx resolves a literal `proxy_pass
http://web:8000` — or an `upstream { server web:8000; }` block — exactly once,
when it starts, and pins the single IP it receives for the life of the process.
Every request then lands on the same replica, and it looks like nothing is
happening while the configuration appears perfectly correct. The fix is in
`nginx/default.conf`: point `proxy_pass` at a *variable*, which defers DNS
resolution to request time, and declare Docker's embedded DNS server
(`resolver 127.0.0.11`). Docker publishes one A record per replica and rotates
them, so each request gets a different one.

### State lives outside the containers

The app counts visits into Redis. The backend is chosen at runtime by whether
`REDIS_URL` is set — the image is identical either way.

Without Redis, each container counts on its own and the number jumps about as
requests land on different replicas. With Redis, the replicas share one number:

```bash
for i in $(seq 1 5); do curl -s localhost:8085/api/info | grep -o '"visits":[0-9]*'; done
```

The count climbs 1, 2, 3, 4, 5 even though the hostname keeps changing. The app
reaches Redis at `redis://redis:6379/0` — a **service name**, not an address.
Docker's embedded DNS resolves it on the compose network, which is why the same
image finds Redis in any environment that provides a host called `redis`.

The named volume is what actually persists:

```bash
docker compose down                        # containers destroyed, volume kept
docker compose up -d --scale web=3
curl -s localhost:8085/api/info | grep -o '"visits":[0-9]*'   # carries on

docker compose down -v                     # -v destroys the volume too
```

**When Redis is down the app degrades, it does not fail.** `increment()` catches
the connection error, the page reports `redis (unavailable)`, and both probes
still return 200 — because the application *is* healthy; only a feature is
missing. A probe that reported failure there would pull perfectly good replicas
out of rotation all at once, turning one broken dependency into a total outage.

### Liveness and readiness are different questions

- **`/health/live`** — is this process wedged? A failure means *restart me*. It
  deliberately checks nothing else: a dependency check in a liveness probe turns
  an outage into a restart loop across every replica simultaneously.
- **`/health/ready`** — should this instance be sent traffic right now? A failure
  means *take me out of the load balancer, but leave me running*.

What fails readiness is shutdown. A preStop hook drops a marker file and then
waits, so the orchestrator removes the instance from its endpoint list before
the server stops accepting connections. Without that gap the endpoints still
name a process that has already closed its listener, and a handful of requests
fail on every single deploy.

## Resource limits

A container is not a small virtual machine. It is an ordinary process on the
host kernel, fenced in by **namespaces** (what it can see) and **cgroups** (what
it can use). The kernel exposes the second half as files, so the process can
read its own limits:

```bash
docker run -d --name wair-limited -m 128m --cpus 0.5 -p 8086:8000 \
  georgelukaanya/where-am-i-running:latest
curl -s localhost:8086/api/info
```

Reports `128 MB` and `0.5 CPUs`. Run it without those flags and both say
`unlimited` — same image, nothing about the app changed. `app/limits.py` reads
cgroup v2 (`memory.max`, `cpu.max`) and falls back to the v1 layout.

The limit is real, not a label — this gets killed:

```bash
docker run --rm -m 32m georgelukaanya/where-am-i-running:latest \
  python -c "x = bytearray(200 * 1024 * 1024)"
echo "exit code: $?"   # 137 = SIGKILL, the OOM killer
```

## Graceful shutdown

`docker stop` sends **SIGTERM**, waits 10 seconds, then **SIGKILL**s. A well
behaved container uses that window to finish the requests it is already
serving. `/api/slow` makes it observable:

```bash
docker run -d --name wair-drain -p 8087:8000 georgelukaanya/where-am-i-running:latest
curl -s "localhost:8087/api/slow?seconds=8" &   # a request in flight
sleep 1
docker stop -t 40 wair-drain                    # ask it to stop, politely
docker logs wair-drain
```

The logs show `Handling signal: term`, the worker draining, then
`shutdown complete` — and the backgrounded `curl` still gets its answer.

Two details make that work:

- `CMD ["sh", "-c", "exec gunicorn ..."]` — **`exec` matters.** Without it the
  shell remains PID 1, and a shell does not forward signals to its children, so
  gunicorn would never hear the SIGTERM and the container would be killed
  mid-request 10 seconds later. This is the single most common reason a
  containerised app drops requests on every deploy.
- `graceful_timeout = 30` in `gunicorn.conf.py` sets how long workers may take
  to finish. It exceeds Docker's 10-second grace period, so a genuinely slow
  drain needs `docker stop -t 40`.

## Metrics

`/metrics` publishes request rates, latency histograms and error counts in
Prometheus exposition format, plus:

- `app_build_info` — a gauge pinned at 1 whose labels carry the running commit,
  image tag and environment. Prometheus has no string values, so this is the
  conventional way to publish build metadata; a dashboard can display the
  running commit and an alert can notice a rollback.
- `app_counter_available` — 1 when the visit counter backend answered, 0 when
  it did not.
- `app_visits` — the shared visit count.

Health checks are excluded from the request metrics. They fire every 30 seconds
forever, and counting them would swamp the request-rate graph with traffic
nobody sent.

Under gunicorn each worker is a separate process with its own counters, so a
naive `/metrics` would return whichever worker served the scrape and the numbers
would appear to jump about at random. `PROMETHEUS_MULTIPROC_DIR` puts the
workers' counters in a shared directory that the endpoint sums across, and
`child_exit` in `gunicorn.conf.py` retires a dead worker's gauges so they are
not counted forever.

## Publishing to a registry

```bash
docker login -u georgelukaanya          # use an access token, not a password
docker push georgelukaanya/where-am-i-running:latest
```

Published at `docker.io/georgelukaanya/where-am-i-running`.

## Continuous integration

`.github/workflows/docker-build.yml` runs three jobs on every push and pull
request to `main`.

**`test`** — pytest.

**`smoke`** — starts the whole compose stack with three replicas, waits for the
proxy, then asserts that a dozen requests reach more than one container and that
the visit count increments by exactly one across replicas. Unit tests prove the
Python is right; this proves the *stack* is right. Without it a broken nginx
config or compose file would sail through CI untouched, because no unit test
loads either file.

**`docker`** — builds, scans and publishes:

- **Vulnerability scanning.** Trivy fails the build on HIGH or CRITICAL
  findings. It cannot scan a multi-architecture manifest, and cannot scan an
  image that only exists in a registry, so the job builds one architecture into
  the local daemon first (`load: true`), scans that, and pushes afterwards — the
  layer cache makes the second build nearly free. `ignore-unfixed: true`
  matters: `python:3.12-slim` always carries some Debian CVEs with no fix
  released, and failing on those just trains everyone to ignore the gate.
- **Multi-architecture builds.** QEMU emulation plus
  `platforms: linux/amd64,linux/arm64` produces one tag that works on x86
  servers *and* Apple Silicon or Raspberry Pi.
- **SBOM and provenance.** A CycloneDX software bill of materials is uploaded as
  a workflow artifact, and `sbom: true` / `provenance: mode=max` attach
  attestations to the pushed image, so anyone can ask what is inside it and
  which commit and workflow built it.
- **Pull requests build and scan, but never push.**

Publishing needs two repository secrets — `DOCKERHUB_USERNAME` and
`DOCKERHUB_TOKEN` (an access token from hub.docker.com → Account Settings →
Personal access tokens, Read & Write).

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -v
```
