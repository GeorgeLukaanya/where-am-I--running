# syntax=docker/dockerfile:1

# --- Stage 1: build the dependency tree in a throwaway layer -----------------
FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# --- Stage 2: the runtime image, without pip's build leftovers ---------------
FROM python:3.12-slim AS runtime

# Build metadata: passed in by the pipeline, surfaced by the app at runtime.
ARG GIT_SHA=unknown
ARG IMAGE_TAG=local

LABEL org.opencontainers.image.title="where-am-i-running" \
      org.opencontainers.image.source="https://github.com/GeorgeLukaanya/where-am-I--running" \
      org.opencontainers.image.revision="${GIT_SHA}"

ENV GIT_SHA=${GIT_SHA} \
    IMAGE_TAG=${IMAGE_TAG} \
    APP_ENV=production \
    PORT=8000 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /install /usr/local

RUN useradd --create-home --uid 10001 appuser
WORKDIR /srv/app
COPY app ./app
COPY wsgi.py .
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status == 200 else 1)"

CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT} --workers 2 --access-logfile - wsgi:app"]
