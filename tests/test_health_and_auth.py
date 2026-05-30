"""Tests for the deep /health endpoint and the optional admin bearer-token."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import evk.api as api_module
from evk.api import app


def test_health_returns_200_when_all_green(monkeypatch):
    # Stub the Gemini healthcheck so the test never calls the real API.
    # The health endpoint does: get_gemini().healthcheck() — patch the cached
    # instance's method directly via api_module's imported reference.
    import evk.api as _api
    monkeypatch.setattr(_api, "get_gemini", lambda: type("_G", (), {"healthcheck": lambda self: True})())
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["repos"]["ok"] is True
    assert body["checks"]["gemini"]["ok"] is True


def test_healthz_is_cheap_and_always_200():
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_health_returns_503_when_repos_unavailable(monkeypatch):
    """If repos can't even list, /health must 503 — readiness gating."""

    class _BrokenOpps:
        @staticmethod
        def list_all(*_args, **_kwargs):
            raise RuntimeError("firestore offline")

    class _BrokenRepos:
        opportunities = _BrokenOpps()

    monkeypatch.setattr(api_module, "get_repos", lambda: _BrokenRepos())
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["status"] == "degraded"
    assert detail["checks"]["repos"]["ok"] is False


@pytest.fixture
def _with_admin_token(monkeypatch):
    monkeypatch.setattr(api_module.get_settings(), "admin_api_token", "super-secret", raising=False)
    return "super-secret"


def test_admin_api_blocked_without_token(_with_admin_token):
    assert _with_admin_token
    with TestClient(app) as client:
        resp = client.get("/admin/drafts")
    assert resp.status_code == 401


def test_admin_api_allowed_with_correct_token(_with_admin_token):
    with TestClient(app) as client:
        resp = client.get("/admin/drafts", headers={"Authorization": f"Bearer {_with_admin_token}"})
    assert resp.status_code == 200


def test_admin_api_rejects_wrong_token(_with_admin_token):
    assert _with_admin_token
    with TestClient(app) as client:
        resp = client.get("/admin/drafts", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_all_admin_endpoints_share_auth(_with_admin_token):
    """Every /admin/* route must enforce the token (router-level dep)."""
    assert _with_admin_token
    with TestClient(app) as client:
        for path in ("/admin/drafts", "/admin/opportunities", "/admin/students"):
            assert client.get(path).status_code == 401, path
