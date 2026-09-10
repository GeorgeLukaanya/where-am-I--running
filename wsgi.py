"""WSGI entry point used by gunicorn in the container."""

from app import create_app

app = create_app()
