# Where am I running?

BSE 4105 — Software Integration and Deployment · Exercise 6 (Containerisation)
Lukaanya George

A small Flask web app that reports the environment it is running in: the
hostname (inside a container, that is the container ID), the image tag, the
git commit the image was built from, the Python version, and its uptime.
Everything that varies between builds and deployments arrives through
environment variables, so a single image can run anywhere without a rebuild.

| Route | Purpose |
| --- | --- |
| `GET /` | HTML card showing the runtime details |
| `GET /api/info` | The same details as JSON |
| `GET /health` | `{"status": "ok"}` — used by the container `HEALTHCHECK` and by CI |

## The four exercise steps

### 1. The Dockerfile

Multi-stage, on `python:3.12-slim`:

- the **builder** stage installs dependencies into `/install`, so pip's caches
  and build leftovers never reach the final image;
- the **runtime** stage copies that prefix in, adds the application, and runs
  as the unprivileged `appuser` (uid 10001) rather than root;
- `ARG GIT_SHA` / `ARG IMAGE_TAG` become both OCI image labels and runtime
  environment variables, which is how the pipeline stamps a build into the app;
- `HEALTHCHECK` polls `/health` so Docker itself reports the container healthy;
- gunicorn serves the app — the Flask development server is not meant for
  anything but development.

Resulting image: **134 MB**.

### 2. Build it and run it locally

```bash
docker build -t georgelukaanya/where-am-i-running:local \
  --build-arg GIT_SHA=$(git rev-parse --short HEAD) \
  --build-arg IMAGE_TAG=georgelukaanya/where-am-i-running:local .

docker run -d --name wair -p 8081:8000 georgelukaanya/where-am-i-running:local

curl -s localhost:8081/health    # {"status":"ok"}
curl -s localhost:8081/api/info  # full JSON
xdg-open http://localhost:8081   # the HTML card
```

Two containers from the same image, configured differently:

```bash
docker run -d --name wair-a -p 8082:8000 -e APP_ENV=staging    georgelukaanya/where-am-i-running:local
docker run -d --name wair-b -p 8083:8000 -e APP_ENV=production georgelukaanya/where-am-i-running:local
curl -s localhost:8082/api/info
curl -s localhost:8083/api/info
```

Each reports a different hostname and a different environment — one image,
many instances, no rebuild.

Clean up: `docker rm -f wair wair-a wair-b`

### 3. Push the image to a registry

Registry: **Docker Hub**, `docker.io/georgelukaanya/where-am-i-running`.

```bash
docker login -u georgelukaanya          # use an access token, not the password
docker tag georgelukaanya/where-am-i-running:local georgelukaanya/where-am-i-running:latest
docker push georgelukaanya/where-am-i-running:latest
```

### 4. The docker build job in the pipeline

`.github/workflows/docker-build.yml` runs on every push and pull request to
`main`, in two jobs:

1. **test** — installs the dependencies and runs `pytest`;
2. **docker** — needs `test`, then builds the image with buildx and pushes it
   to Docker Hub tagged `latest` and `sha-<short-commit>`. The real commit SHA
   is passed in as a build argument, so the deployed app can tell you exactly
   which commit it came from.

Pull requests build the image but **do not push it** — the build is verified
without publishing anything.

#### Repository secrets required

On GitHub: *Settings → Secrets and variables → Actions → New repository secret*

| Secret | Value |
| --- | --- |
| `DOCKERHUB_USERNAME` | `georgelukaanya` |
| `DOCKERHUB_TOKEN` | An access token from hub.docker.com → Account Settings → Personal access tokens (Read & Write) |

## Running the tests locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -v
```

## Beyond the exercise

The four steps above are the graded exercise. What follows demonstrates what
containers are actually *for* — the plain `docker build` / `docker run`
commands above keep working unchanged.

### Load balancing across replicas

`docker-compose.yml` runs the app behind an nginx reverse proxy, so the same
image can be scaled to several containers sharing one address.

```bash
docker compose up -d --build --scale web=3
docker compose ps          # 3 web replicas + 1 proxy
```

The stack is served on **http://localhost:8085** (the proxy). The replicas
themselves publish no host port — they are reachable only inside the Docker
network, which is how this is normally arranged.

```bash
for i in $(seq 1 6); do curl -s localhost:8085/api/info | grep -o '"hostname":"[^"]*"'; done
```

More than one container ID comes back: requests are being spread across the
replicas. Open http://localhost:8085 and hold refresh to watch the Hostname
row change.

```bash
docker compose down        # stop the stack
```

**One trap worth knowing about.** nginx resolves a literal `proxy_pass
http://web:8000` — or an `upstream { server web:8000; }` block — exactly once,
when it starts, and pins the single IP it receives for the life of the
process. Every request then lands on the same replica, and the demo looks like
it is doing nothing while the configuration appears perfectly correct. The fix
is in `nginx/default.conf`: point `proxy_pass` at a *variable*, which defers
DNS resolution to request time, and declare Docker's embedded DNS server
(`resolver 127.0.0.11`). Docker publishes one A record per replica and rotates
them, so each request gets a different one.

### A second service: shared state in Redis

The compose stack also runs a Redis container, and the app counts visits into
it. The counter backend is chosen at runtime by whether `REDIS_URL` is set —
the image is identical either way.

**Without Redis, state is per-container.** Run a few replicas with no Redis and
the count jumps about, because each container is counting on its own:

```bash
docker compose run --rm -e REDIS_URL= -p 8086:8000 web   # one replica, no Redis
curl -s localhost:8086/api/info    # "counter_backend": "in-memory"
```

**With Redis, the replicas share one number.** That is the whole stack:

```bash
docker compose up -d --build --scale web=3
for i in $(seq 1 5); do curl -s localhost:8085/api/info | grep -o '"visits":[0-9]*'; done
```

The count climbs 1, 2, 3, 4, 5 even though the hostname keeps changing — three
separate containers, one shared counter. The app reaches it at
`redis://redis:6379/0`: a **service name**, not an address. Docker's embedded
DNS resolves it on the compose network, which is why the same image finds Redis
in any environment that provides a host called `redis`.

**The volume is what actually persists.** Redis writes to `/data`, which is a
named volume rather than the container's writable layer:

```bash
docker compose down                        # containers destroyed, volume kept
docker compose up -d --scale web=3
curl -s localhost:8085/api/info | grep -o '"visits":[0-9]*'   # carries on

docker compose down -v                     # -v also destroys the volume
docker compose up -d --scale web=3
curl -s localhost:8085/api/info | grep -o '"visits":[0-9]*'   # back to 1
```

**When Redis is down the app degrades, it does not fail.** `increment()`
catches the connection error, the page reports `redis (unavailable)`, and
`/health` still returns 200 — because the application *is* healthy; only a
feature is missing. A health check that reported failure here would have the
load balancer pull a perfectly good container out of rotation, and in a real
outage that turns one broken dependency into a total one.

### Resource limits, and what a container actually is

A container is not a small virtual machine. It is an ordinary process on the
host kernel, fenced in by **namespaces** (what it can see) and **cgroups**
(what it can use). The kernel exposes the second half as files, so the process
can read its own limits — which is why the page can show them:

```bash
docker run -d --name wair-limited -m 128m --cpus 0.5 -p 8086:8000 \
  georgelukaanya/where-am-i-running:latest
curl -s localhost:8086/api/info | grep -o '"\(memory\|cpu\)_limit":"[^"]*"'
```

Reports `128 MB` and `0.5 CPUs`. Run it without those flags and both say
`unlimited` — same image, and nothing about the app changed. `app/limits.py`
reads cgroup v2 (`memory.max`, `cpu.max`) and falls back to the v1 layout.

Proof it is a real limit rather than a label — this gets killed:

```bash
docker run --rm -m 32m georgelukaanya/where-am-i-running:latest \
  python -c "x = bytearray(200 * 1024 * 1024)"
echo "exit code: $?"   # 137 = SIGKILL, the OOM killer
```

### Graceful shutdown

`docker stop` sends **SIGTERM**, waits 10 seconds, then **SIGKILL**s. A well
behaved container uses that window to finish the requests it is already
serving. `/api/slow` exists to make it observable:

```bash
docker run -d --name wair-drain -p 8087:8000 georgelukaanya/where-am-i-running:latest
curl -s "localhost:8087/api/slow?seconds=8" &   # a request in flight
sleep 1
docker stop wair-drain                          # ask it to stop, politely
docker logs wair-drain
```

The logs show `Handling signal: term`, the worker draining, then
`shutdown complete` — and the backgrounded `curl` still gets its answer.

Two details in the Dockerfile make that work:

- `CMD ["sh", "-c", "exec gunicorn ..."]` — **`exec` matters.** Without it the
  shell remains PID 1, and a shell does not forward signals to its children,
  so gunicorn would never hear the SIGTERM and the container would be killed
  mid-request 10 seconds later. This is the single most common reason a
  containerised app drops requests on every deploy.
- `graceful_timeout = 30` in `gunicorn.conf.py` sets how long workers may take
  to finish. Note it exceeds Docker's 10-second grace period, so for a genuinely
  slow drain you would stop the container with `docker stop -t 40`.

### A hardened pipeline

`.github/workflows/docker-build.yml` now runs three jobs in sequence:

**`test`** — pytest, as before.

**`smoke`** — starts the whole compose stack with three replicas, waits for the
proxy, then asserts that a dozen requests reach more than one container and
that the visit count increments by exactly one across replicas. Unit tests
prove the Python is right; this proves the *stack* is right. Without it a
broken nginx config or compose file would sail through CI untouched, because
no unit test loads either file.

**`docker`** — builds, scans, and publishes:

- **Vulnerability scanning.** Trivy fails the build on HIGH or CRITICAL
  findings. It cannot scan a multi-architecture manifest, and cannot scan an
  image that only exists in a registry, so the job builds one architecture into
  the local daemon first (`load: true`), scans that, and pushes afterwards —
  the layer cache makes the second build nearly free. `ignore-unfixed: true`
  matters: `python:3.12-slim` always carries some Debian CVEs with no fix
  released, and failing on those just trains everyone to ignore the gate.
- **Multi-architecture builds.** QEMU emulation plus
  `platforms: linux/amd64,linux/arm64` produces one tag that works on x86
  servers *and* Apple Silicon or Raspberry Pi. Docker Hub shows both under the
  same tag; the client picks the right one automatically.
- **SBOM and provenance.** A CycloneDX software bill of materials is uploaded
  as a workflow artifact, and `sbom: true` / `provenance: mode=max` attach
  attestations to the pushed image — so anyone can ask what is inside it and
  which commit and workflow built it. This is what "supply chain security"
  means in practice.
- **Pull requests build and scan, but never push.** A review gets the
  verification without anything being published.
