"""Shared view models and render helpers for UI routes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from evk.config import Settings, get_settings
from evk.factory import describe_wiring
from evk.firestore_repo import Repos
from evk.models import AppUser, DraftMessage, DraftStatus, Opportunity, StudentLevel, UserRole
from evk.ui.deps import redirect
from evk.ui.template_env import templates

STATUS_TABS: list[tuple[str, str]] = [
    ("Pending", "pending_approval"),
    ("Approved", "approved"),
    ("Sent", "sent"),
    ("Rejected", "rejected"),
    ("Failed", "failed"),
]

_STATUS_LITERAL = Literal["pending_approval", "approved", "sent", "rejected", "failed"]


@dataclass(slots=True)
class OpportunityView:
    inner: Opportunity
    days_until: int | None
    match_score: float | None = None
    match_reasons: list[str] | None = None

    @property
    def title(self) -> str:
        return self.inner.title

    @property
    def kind(self):
        return self.inner.kind

    @property
    def organization(self) -> str:
        return self.inner.organization

    @property
    def deadline(self) -> datetime | None:
        return self.inner.deadline


def decorate_opps(opps: list[Opportunity]) -> list[OpportunityView]:
    today = datetime.now(UTC).date()
    out: list[OpportunityView] = []
    for opp in opps:
        days: int | None = None
        if opp.deadline is not None:
            delta = (opp.deadline.date() - today).days
            days = delta if delta >= 0 else None
        out.append(OpportunityView(inner=opp, days_until=days))
    out.sort(key=lambda item: (item.days_until is None, item.days_until or 10**9))
    return out


def stats(repos: Repos) -> dict[str, int]:
    today = date.today()
    drafts = repos.drafts.list_all()
    pending = sum(1 for draft in drafts if draft.status == DraftStatus.PENDING_APPROVAL)
    sent_today = sum(
        1
        for draft in drafts
        if draft.status == DraftStatus.SENT and draft.sent_at and draft.sent_at.date() == today
    )
    return {
        "pending": pending,
        "sent_today": sent_today,
        "opportunities": len(repos.opportunities.list_all()),
        "students": len(repos.students.list_all()),
        "users": len(repos.users.list_all()),
    }


def status_counts(repos: Repos) -> dict[str, int]:
    counts = dict.fromkeys((value for _, value in STATUS_TABS), 0)
    for draft in repos.drafts.list_all():
        counts[draft.status.value] = counts.get(draft.status.value, 0) + 1
    return counts


def role_summary(repos: Repos) -> dict[str, int]:
    counts = {role.value: 0 for role in UserRole}
    active = 0
    for user in repos.users.list_all():
        counts[user.role.value] += 1
        if user.is_active:
            active += 1
    counts["active"] = active
    return counts


def setup_status(repos: Repos, wiring: dict[str, str], settings: Settings) -> dict[str, object]:
    raw_count = len(repos.raw_emails.list_all(limit=1))
    sent_any = bool(repos.drafts.list_by_status(DraftStatus.SENT, limit=1))
    real_students = len(repos.students.list_all()) > 4
    newsletter_received = raw_count > 0
    real_opps = len(repos.opportunities.list_all()) > 14
    return {
        "gmail_ok": wiring.get("inkbox") == "gmail",
        "gemini_ok": wiring.get("gemini") != "stub",
        "password_changed": settings.auth_local_demo_password != "ChangeMe123!",
        "students_imported": real_students,
        "newsletter_received": newsletter_received or real_opps,
        "first_approval_done": sent_any,
        "phase": (
            1 if not (wiring.get("inkbox") == "gmail" and wiring.get("gemini") != "stub") else
            2 if not (newsletter_received or real_opps) else
            3 if not real_students else
            4 if not sent_any else
            5
        ),
    }


def dashboard_context(repos: Repos, current_user: AppUser) -> dict[str, object]:
    wiring = describe_wiring()
    settings = get_settings()
    return {
        "request": None,
        "current_user": current_user,
        "wiring": wiring,
        "stats": stats(repos),
        "setup": setup_status(repos, wiring, settings),
        "status_counts": status_counts(repos),
        "students": repos.students.list_all(limit=50),
        "opportunities": decorate_opps(repos.opportunities.list_all(limit=100)),
        "status_tabs": STATUS_TABS,
        "active_status": "pending_approval",
        "flash": None,
        "users": sorted(repos.users.list_all(limit=200), key=lambda user: (user.role.value, user.name)),
        "role_summary": role_summary(repos),
        "levels": list(StudentLevel),
    }


def render_drafts(request: Request, repos: Repos, active_status: str) -> HTMLResponse:
    try:
        status = DraftStatus(active_status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid status") from exc
    drafts = repos.drafts.list_by_status(status, limit=200)
    drafts.sort(key=lambda draft: draft.created_at, reverse=True)
    opportunity_map = {
        draft.opportunity_id: repos.opportunities.get(draft.opportunity_id) for draft in drafts
    }
    return templates.TemplateResponse(
        request,
        "_drafts.html",
        {
            "request": request,
            "drafts": drafts,
            "active_status": active_status,
            "opportunity_map": opportunity_map,
        },
    )


def draft_panel_context(
    request: Request,
    repos: Repos,
    active_status: str,
    current_user: AppUser,
) -> dict[str, object]:
    drafts_response = render_drafts(request, repos, active_status)
    return {
        "request": request,
        "current_user": current_user,
        "active_status": active_status,
        "status_tabs": STATUS_TABS,
        "status_counts": status_counts(repos),
        "drafts": drafts_response.context["drafts"],
        "opportunity_map": drafts_response.context["opportunity_map"],
    }


def render_drafts_panel(
    request: Request,
    repos: Repos,
    active_status: str,
    current_user: AppUser,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_drafts_panel.html",
        draft_panel_context(request, repos, active_status, current_user),
    )


def render_stats(
    request: Request,
    repos: Repos,
    current_user: AppUser,
    flash: str | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_stats.html",
        {
            "request": request,
            "current_user": current_user,
            "stats": stats(repos),
            "flash": flash,
            "wiring": describe_wiring(),
        },
    )


def render_user_panel(
    request: Request,
    repos: Repos,
    current_user: AppUser,
    flash: str | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_users_panel.html",
        {
            "request": request,
            "current_user": current_user,
            "users": sorted(repos.users.list_all(limit=200), key=lambda user: (user.role.value, user.name)),
            "role_summary": role_summary(repos),
            "flash": flash,
        },
    )


def render_fragment(template_name: str, context: dict[str, object]) -> str:
    return templates.get_template(template_name).render(context)


def render_drafts_with_stats(
    request: Request,
    repos: Repos,
    active_status: str,
    current_user: AppUser,
    *,
    flash: str | None = None,
) -> HTMLResponse:
    drafts_panel_html = render_fragment(
        "_drafts_panel.html",
        draft_panel_context(request, repos, active_status, current_user),
    )
    stats_html = render_fragment(
        "_stats.html",
        {
            "request": request,
            "current_user": current_user,
            "stats": stats(repos),
            "flash": flash,
            "wiring": describe_wiring(),
        },
    )
    body = (
        '<section id="stats" class="mt-6" hx-get="/ui/stats" '
        'hx-trigger="every 10s" hx-swap="innerHTML" hx-swap-oob="outerHTML:#stats">'
        f"{stats_html}</section>"
        f"{drafts_panel_html}"
    )
    return HTMLResponse(body)


def staff_redirect(request: Request, user: AppUser | None) -> RedirectResponse | None:
    if user is None:
        return redirect(request, "login_page")
    if user.role is UserRole.ADMIN:
        return None
    return redirect(request, "app_home")


def staff_or_ngo_redirect(request: Request, user: AppUser | None) -> RedirectResponse | None:
    if user is None:
        return redirect(request, "login_page")
    if user.role in (UserRole.ADMIN, UserRole.NGO_ADMIN):
        return None
    return redirect(request, "app_home")


def role_home(user: AppUser) -> str:
    if user.role is UserRole.ADMIN:
        return "admin_dashboard"
    if user.role is UserRole.NGO_ADMIN:
        return "admin_dashboard"
    return "student_dashboard"


def auth_page_context(
    request: Request,
    *,
    auth_view: Literal["existing", "new"],
    flash: str | None = None,
) -> dict[str, object]:
    return {
        "request": request,
        "current_user": None,
        "wiring": describe_wiring(),
        "flash": flash,
        "auth_view": auth_view,
    }


# Backward-compatible aliases for tests
_decorate_opps = decorate_opps

__all__ = [
    "STATUS_TABS",
    "OpportunityView",
    "_STATUS_LITERAL",
    "_decorate_opps",
    "auth_page_context",
    "dashboard_context",
    "decorate_opps",
    "draft_panel_context",
    "render_drafts_panel",
    "render_drafts_with_stats",
    "render_stats",
    "render_user_panel",
    "role_home",
    "staff_or_ngo_redirect",
    "staff_redirect",
    "stats",
]
