"""Tests for the "Where am I running?" app."""

import socket

import pytest

from app import create_app


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
