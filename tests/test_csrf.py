"""CSRF middleware tests."""

from __future__ import annotations

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from evk.api import app
from evk.ui.csrf import CSRF_COOKIE, CSRF_FORM_FIELD, CSRF_HEADER, verify_csrf_request


def test_verify_csrf_rejects_missing_token():
    scope = {"type": "http", "headers": [], "method": "POST", "path": "/"}
    request = Request(scope)
    with pytest.raises(HTTPException) as exc:
        verify_csrf_request(request, None)
    assert exc.value.status_code == 403


def test_post_with_csrf_cookie_and_field_succeeds():
    with TestClient(app) as client:
        client.get("/login")
        token = client.cookies[CSRF_COOKIE]
        resp = client.post(
            "/auth/login",
            data={"email": "nobody@example.com", "access_key": "wrong", CSRF_FORM_FIELD: token},
            headers={CSRF_HEADER: token},
        )
        assert resp.status_code in {200, 400}


def test_rest_admin_poll_exempt_without_csrf_cookie():
    with TestClient(app) as client:
        from fastapi.testclient import TestClient as TC

        original = TC.post
        TC.post = original  # use patched version but no prior GET — exempt path
        resp = client.post("/admin/poll")
        assert resp.status_code == 200
