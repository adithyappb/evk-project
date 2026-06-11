"""FastAPI dependencies shared across UI route modules."""

from __future__ import annotations

from fastapi import Depends, Request

from evk.agents.distributor import DistributorAgent
from evk.agents.ingestion import IngestionAgent
from evk.agents.reminder import ReminderAgent
from evk.auth import AuthService, build_auth_notifier
from evk.config import Settings, get_settings
from evk.factory import get_inkbox, get_repos
from evk.firestore_repo import Repos
from evk.inkbox_client import InkboxClient
from evk.models import AppUser, UserRole
from fastapi import HTTPException
from fastapi.responses import RedirectResponse


def repos_dep() -> Repos:
    return get_repos()


def inkbox_dep() -> InkboxClient:
    return get_inkbox()


def settings_dep() -> Settings:
    return get_settings()


def auth_dep(
    repos: Repos = Depends(repos_dep),
    settings: Settings = Depends(settings_dep),
) -> AuthService:
    service = AuthService(repos=repos, notifier=build_auth_notifier(settings), settings=settings)
    service.ensure_bootstrap()
    return service


def ingestion_dep(
    repos: Repos = Depends(repos_dep), inkbox: InkboxClient = Depends(inkbox_dep)
) -> IngestionAgent:
    return IngestionAgent(repos=repos, inkbox=inkbox)


def distributor_dep(
    repos: Repos = Depends(repos_dep), inkbox: InkboxClient = Depends(inkbox_dep)
) -> DistributorAgent:
    return DistributorAgent(repos=repos, inkbox=inkbox)


def reminder_dep(
    repos: Repos = Depends(repos_dep), inkbox: InkboxClient = Depends(inkbox_dep)
) -> ReminderAgent:
    return ReminderAgent(repos=repos, inkbox=inkbox)


def session_cookie_name(settings: Settings) -> str:
    return settings.session_cookie_name


def current_user(
    request: Request,
    auth: AuthService = Depends(auth_dep),
    settings: Settings = Depends(settings_dep),
) -> AppUser | None:
    return auth.get_session_user(request.cookies.get(session_cookie_name(settings)))


def redirect(request: Request, route_name: str, **params: str) -> RedirectResponse:
    return RedirectResponse(request.url_for(route_name, **params), status_code=303)


def clear_session(response: RedirectResponse, settings: Settings) -> None:
    response.delete_cookie(settings.session_cookie_name, path="/")


def set_session_cookie(response: RedirectResponse, session_id: str, settings: Settings) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        session_id,
        httponly=True,
        samesite="lax",
        secure=settings.app_env == "prod",
        max_age=settings.session_ttl_hours * 3600,
        path="/",
    )


def staff_required(user: AppUser | None) -> AppUser:
    if user is None or user.role is not UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="admin access required")
    return user


# Backward-compatible aliases for tests and dependency overrides
_repos_dep = repos_dep
_inkbox_dep = inkbox_dep
_settings_dep = settings_dep
_auth_dep = auth_dep
_ingestion_dep = ingestion_dep
_distributor_dep = distributor_dep
_reminder_dep = reminder_dep
_current_user = current_user
_redirect = redirect
_clear_session = clear_session
_set_session_cookie = set_session_cookie
_staff_required = staff_required

__all__ = [
    "_auth_dep",
    "_clear_session",
    "_current_user",
    "_distributor_dep",
    "_ingestion_dep",
    "_inkbox_dep",
    "_redirect",
    "_reminder_dep",
    "_repos_dep",
    "_set_session_cookie",
    "_settings_dep",
    "_staff_required",
    "auth_dep",
    "clear_session",
    "current_user",
    "distributor_dep",
    "ingestion_dep",
    "inkbox_dep",
    "redirect",
    "reminder_dep",
    "repos_dep",
    "session_cookie_name",
    "set_session_cookie",
    "settings_dep",
    "staff_required",
]
