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
