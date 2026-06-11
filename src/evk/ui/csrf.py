"""Double-submit CSRF protection for HTML forms and HTMX POSTs."""

from __future__ import annotations

import secrets
from typing import Final

from fastapi import Form, HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from evk.config import get_settings

CSRF_COOKIE: Final[str] = "evk_csrf"
CSRF_FORM_FIELD: Final[str] = "csrf_token"
CSRF_HEADER: Final[str] = "X-CSRF-Token"
SAFE_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_token_from_request(request: Request) -> str:
    token = request.cookies.get(CSRF_COOKIE)
    if token:
        return token
    return getattr(request.state, "csrf_token", "")


def verify_csrf_request(request: Request, submitted: str | None) -> None:
    expected = request.cookies.get(CSRF_COOKIE) or getattr(request.state, "csrf_token", None)
    if not expected or not submitted or not secrets.compare_digest(expected, submitted):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed")


def submitted_csrf(request: Request, csrf_token: str | None = Form(default=None)) -> str | None:
    return csrf_token or request.headers.get(CSRF_HEADER)


def require_csrf(
    request: Request,
    csrf_token: str | None = Form(default=None),
) -> None:
    if request.method in SAFE_METHODS:
        return
    verify_csrf_request(request, submitted_csrf(request, csrf_token))


def csrf_exempt(request: Request) -> bool:
    path = request.url.path
    if path.startswith("/webhooks/") or path in {"/health", "/healthz"}:
        return True
    if request.headers.get("authorization", "").lower().startswith("bearer "):
        return True
    # JSON REST admin API (no browser CSRF cookie) — see evk.api admin router
    if not request.cookies.get(CSRF_COOKIE):
        rest_prefixes = ("/admin/drafts", "/admin/opportunities", "/admin/students", "/admin/poll")
        if any(path.startswith(prefix) for prefix in rest_prefixes):
            return True
        if "application/json" in request.headers.get("content-type", ""):
            return True
    return False


class CsrfMiddleware(BaseHTTPMiddleware):
    """Issue CSRF cookie and validate unsafe methods for browser/UI traffic."""

    async def dispatch(self, request: Request, call_next) -> Response:
        token = request.cookies.get(CSRF_COOKIE)
        if not token:
            token = new_csrf_token()
            request.state.csrf_token = token
            request.state.csrf_set_cookie = True
        else:
            request.state.csrf_token = token

        if request.method not in SAFE_METHODS and not csrf_exempt(request):
            submitted = request.headers.get(CSRF_HEADER)
            if not submitted:
                try:
                    form = await request.form()
                    raw = form.get(CSRF_FORM_FIELD)
                    submitted = str(raw) if raw is not None else None
                except Exception:
                    submitted = None
            verify_csrf_request(request, submitted)

        response = await call_next(request)
        if getattr(request.state, "csrf_set_cookie", False):
            settings = get_settings()
            response.set_cookie(
                CSRF_COOKIE,
                token,
                httponly=False,
                samesite="lax",
                secure=settings.app_env == "prod",
                max_age=86400 * 7,
                path="/",
            )
        return response


__all__ = [
    "CSRF_COOKIE",
    "CSRF_FORM_FIELD",
    "CSRF_HEADER",
    "CsrfMiddleware",
    "csrf_exempt",
    "csrf_token_from_request",
    "new_csrf_token",
    "require_csrf",
    "verify_csrf_request",
]
