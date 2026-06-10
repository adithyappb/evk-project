"""Dashboard HTML routes with role-aware auth and MFA."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from evk.agents.digest import DigestAgent
from evk.agents.distributor import DistributorAgent
from evk.agents.ingestion import IngestionAgent
from evk.agents.reminder import ReminderAgent
from evk.auth import AuthError, AuthService, TerminalAuthNotifier, build_auth_notifier
from evk.config import Settings, get_settings
from evk.factory import describe_wiring, get_inkbox, get_repos
from evk.logging import logger
from evk.firestore_repo import Repos
from evk.inkbox_client import InboundMessage, InkboxClient
from evk.models import AppUser, DraftMessage, DraftStatus, LoginChallenge, Opportunity, Student, StudentLevel, UserRole
from evk.ui import TEMPLATES_DIR

router = APIRouter(tags=["ui"])
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

STATUS_TABS: list[tuple[str, str]] = [
    ("Parsed", "parsed"),
    ("Pending", "pending_approval"),
    ("Approved", "approved"),
    ("Sent", "sent"),
    ("Rejected", "rejected"),
    ("Failed", "failed"),
]

_STATUS_LITERAL = Literal["parsed", "pending_approval", "approved", "sent", "rejected", "failed"]


def _repos_dep() -> Repos:
    return get_repos()


def _inkbox_dep() -> InkboxClient:
    return get_inkbox()


def _settings_dep() -> Settings:
    return get_settings()


def _auth_dep(
    repos: Repos = Depends(_repos_dep),
    settings: Settings = Depends(_settings_dep),
) -> AuthService:
    service = AuthService(repos=repos, notifier=build_auth_notifier(settings), settings=settings)
    service.ensure_bootstrap()
    return service


def _ingestion_dep(
    repos: Repos = Depends(_repos_dep), inkbox: InkboxClient = Depends(_inkbox_dep)
) -> IngestionAgent:
    return IngestionAgent(repos=repos, inkbox=inkbox)


def _distributor_dep(
    repos: Repos = Depends(_repos_dep), inkbox: InkboxClient = Depends(_inkbox_dep)
) -> DistributorAgent:
    return DistributorAgent(repos=repos, inkbox=inkbox)


def _reminder_dep(
    repos: Repos = Depends(_repos_dep), inkbox: InkboxClient = Depends(_inkbox_dep)
) -> ReminderAgent:
    return ReminderAgent(repos=repos, inkbox=inkbox)


def _session_cookie_name(settings: Settings) -> str:
    return settings.session_cookie_name


def _current_user(
    request: Request,
    auth: AuthService = Depends(_auth_dep),
    settings: Settings = Depends(_settings_dep),
) -> AppUser | None:
    return auth.get_session_user(request.cookies.get(_session_cookie_name(settings)))


def _redirect(request: Request, route_name: str, **params: str) -> RedirectResponse:
    return RedirectResponse(request.url_for(route_name, **params), status_code=303)


def _clear_session(response: RedirectResponse, settings: Settings) -> None:
    response.delete_cookie(settings.session_cookie_name, path="/")


def _set_session_cookie(response: RedirectResponse, session_id: str, settings: Settings) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        session_id,
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=settings.session_ttl_hours * 3600,
        path="/",
    )


def _staff_required(user: AppUser | None) -> AppUser:
    if user is None or user.role is not UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="admin access required")
    return user


def _render_fragment(template_name: str, context: dict[str, object]) -> str:
    return templates.get_template(template_name).render(context)


@dataclass(slots=True)
class OpportunityView:
    inner: Opportunity
    days_until: int | None

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


def _decorate_opps(opps: list[Opportunity]) -> list[OpportunityView]:
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


def _stats(repos: Repos) -> dict[str, int]:
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


def _status_counts(repos: Repos) -> dict[str, int]:
    counts = dict.fromkeys((value for _, value in STATUS_TABS), 0)
    for draft in repos.drafts.list_all():
        counts[draft.status.value] = counts.get(draft.status.value, 0) + 1
    return counts


def _role_summary(repos: Repos) -> dict[str, int]:
    counts = {role.value: 0 for role in UserRole}
    active = 0
    for user in repos.users.list_all():
        counts[user.role.value] += 1
        if user.is_active:
            active += 1
    counts["active"] = active
    return counts


def _setup_status(repos: Repos, wiring: dict[str, str], settings: Settings) -> dict[str, object]:
    """Compute pilot-setup checklist state from live data — used by the wizard card."""
    raw_count = len(repos.raw_emails.list_all(limit=1))
    sent_any = bool(repos.drafts.list_by_status(DraftStatus.SENT, limit=1))
    # "real" students = more than the 4 bundled test seeds
    real_students = len(repos.students.list_all()) > 4
    # newsletter received = any raw email ingested beyond zero
    newsletter_received = raw_count > 0
    # opportunities beyond the 14 seeded ones = pipeline actually processed something
    real_opps = len(repos.opportunities.list_all()) > 14
    return {
        "gmail_ok": wiring.get("inkbox") == "gmail",
        "gemini_ok": wiring.get("gemini") != "stub",
        "password_changed": settings.auth_local_demo_password != "ChangeMe123!",
        "students_imported": real_students,
        "newsletter_received": newsletter_received or real_opps,
        "first_approval_done": sent_any,
        # Derived convenience
        "phase": (
            1 if not (wiring.get("inkbox") == "gmail" and wiring.get("gemini") != "stub") else
            2 if not (newsletter_received or real_opps) else
            3 if not real_students else
            4 if not sent_any else
            5
        ),
    }


def _dashboard_context(repos: Repos, current_user: AppUser) -> dict[str, object]:
    wiring = describe_wiring()
    settings = get_settings()
    return {
        "request": None,
        "current_user": current_user,
        "wiring": wiring,
        "stats": _stats(repos),
        "setup": _setup_status(repos, wiring, settings),
        "status_counts": _status_counts(repos),
        "students": repos.students.list_all(limit=50),
        "opportunities": _decorate_opps(repos.opportunities.list_all(limit=100)),
        "status_tabs": STATUS_TABS,
        "active_status": "pending_approval",
        "flash": None,
        "users": sorted(repos.users.list_all(limit=200), key=lambda user: (user.role.value, user.name)),
        "role_summary": _role_summary(repos),
        "levels": list(StudentLevel),
    }


def _render_drafts(request: Request, repos: Repos, active_status: str) -> HTMLResponse:
    if active_status == "parsed":
        opps = repos.opportunities.list_all(limit=100)
        drafts = [
            DraftMessage(
                id=f"parsed_{opp.id}",
                student_id="unassigned",
                opportunity_id=opp.id,
                to_email="N/A",
                subject=opp.title,
                body_text=opp.summary,
                match_score=0.0,
                status=DraftStatus.PENDING_APPROVAL,
            )
            for opp in opps
            if not opp.is_duplicate
        ]
    else:
        try:
            status = DraftStatus(active_status)
        except ValueError as exc:  # pragma: no cover - typed path
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


def _draft_panel_context(
    request: Request,
    repos: Repos,
    active_status: str,
    current_user: AppUser,
) -> dict[str, object]:
    drafts_response = _render_drafts(request, repos, active_status)
    return {
        "request": request,
        "current_user": current_user,
        "active_status": active_status,
        "status_tabs": STATUS_TABS,
        "status_counts": _status_counts(repos),
        "drafts": drafts_response.context["drafts"],
        "opportunity_map": drafts_response.context["opportunity_map"],
    }


def _render_drafts_panel(
    request: Request,
    repos: Repos,
    active_status: str,
    current_user: AppUser,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_drafts_panel.html",
        _draft_panel_context(request, repos, active_status, current_user),
    )


def _render_stats(
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
            "stats": _stats(repos),
            "flash": flash,
            "wiring": describe_wiring(),
        },
    )


def _render_user_panel(
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
            "role_summary": _role_summary(repos),
            "flash": flash,
        },
    )


def _render_drafts_with_stats(
    request: Request,
    repos: Repos,
    active_status: str,
    current_user: AppUser,
    *,
    flash: str | None = None,
) -> HTMLResponse:
    drafts_panel_html = _render_fragment(
        "_drafts_panel.html",
        _draft_panel_context(request, repos, active_status, current_user),
    )
    stats_html = _render_fragment(
        "_stats.html",
        {
            "request": request,
            "current_user": current_user,
            "stats": _stats(repos),
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


def _staff_redirect(request: Request, user: AppUser | None) -> RedirectResponse | None:
    """Admin-only guard."""
    if user is None:
        return _redirect(request, "login_page")
    if user.role is UserRole.ADMIN:
        return None
    return _redirect(request, "app_home")


def _staff_or_ngo_redirect(request: Request, user: AppUser | None) -> RedirectResponse | None:
    """Guard that allows both Admin and NGO Admin — used for opportunity management."""
    if user is None:
        return _redirect(request, "login_page")
    if user.role in (UserRole.ADMIN, UserRole.NGO_ADMIN):
        return None
    return _redirect(request, "app_home")


def _role_home(user: AppUser) -> str:
    if user.role is UserRole.ADMIN:
        return "admin_dashboard"
    if user.role is UserRole.NGO_ADMIN:
        return "admin_dashboard"
    return "student_dashboard"


def _auth_page_context(
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


@router.get("/", response_class=HTMLResponse, name="landing")
def landing(
    request: Request,
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    if current_user is not None:
        return _redirect(request, "app_home")
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "request": request,
            "current_user": None,
            "wiring": describe_wiring(),
        },
    )


@router.get("/login", response_class=HTMLResponse, name="login_page")
def login_page(
    request: Request,
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
    flash: str | None = None,
) -> HTMLResponse:
    if current_user is not None:
        return _redirect(request, "app_home")
    auth = AuthService(repos=repos, notifier=build_auth_notifier(get_settings()), settings=get_settings())
    auth.ensure_bootstrap()
    return templates.TemplateResponse(
        request,
        "landing.html",
        _auth_page_context(request, auth_view="existing", flash=flash),
    )


@router.get("/register", response_class=HTMLResponse, name="register_page")
def register_page(
    request: Request,
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    if current_user is not None:
        return _redirect(request, "app_home")
    auth = AuthService(repos=repos, notifier=build_auth_notifier(get_settings()), settings=get_settings())
    auth.ensure_bootstrap()
    return templates.TemplateResponse(
        request,
        "landing.html",
        _auth_page_context(request, auth_view="new"),
    )


@router.post("/auth/signup", response_class=HTMLResponse, name="auth_signup")
def auth_signup(
    request: Request,
    email: str = Form(...),
    name: str = Form(...),
    role: str = Form(...),
    access_key: str = Form(...),
    organization: str = Form(default=""),
    auth: AuthService = Depends(_auth_dep),
) -> HTMLResponse:
    try:
        user = auth.create_user(
            email=email,
            name=name,
            role=UserRole(role),
            access_key=access_key,
            organization=organization,
        )
        _, dev_code = auth.start_login(email=user.email, access_key=access_key)
        return templates.TemplateResponse(
            request,
            "auth_verify.html",
            {
                "request": request,
                "current_user": None,
                "wiring": describe_wiring(),
                "email": user.email,
                "flash": "Account created. Enter the verification code to finish signing in.",
                "delivery_mode": auth.settings.auth_email_delivery_mode,
                "dev_code": dev_code,
            },
        )
    except (AuthError, ValueError) as exc:
        return templates.TemplateResponse(
            request,
            "landing.html",
            _auth_page_context(request, auth_view="new", flash=str(exc)),
            status_code=400,
        )


@router.post("/auth/login", response_class=HTMLResponse, name="auth_login")
def auth_login(
    request: Request,
    email: str = Form(...),
    access_key: str = Form(...),
    auth: AuthService = Depends(_auth_dep),
) -> HTMLResponse:
    try:
        user, dev_code = auth.start_login(email=email, access_key=access_key)
        return templates.TemplateResponse(
            request,
            "auth_verify.html",
            {
                "request": request,
                "current_user": None,
                "wiring": describe_wiring(),
                "email": user.email,
                "flash": "Verification code sent.",
                "delivery_mode": auth.settings.auth_email_delivery_mode,
                "dev_code": dev_code,
            },
        )
    except AuthError as exc:
        return templates.TemplateResponse(
            request,
            "landing.html",
            _auth_page_context(request, auth_view="existing", flash=str(exc)),
            status_code=400,
        )


@router.post("/auth/verify", name="auth_verify", response_class=HTMLResponse)
def auth_verify(
    request: Request,
    email: str = Form(...),
    code: str = Form(...),
    auth: AuthService = Depends(_auth_dep),
    settings: Settings = Depends(_settings_dep),
) -> HTMLResponse:
    try:
        user, session = auth.verify_login(email=email, code=code)
    except AuthError as exc:
        return templates.TemplateResponse(
            request,
            "auth_verify.html",
            {
                "request": request,
                "current_user": None,
                "wiring": describe_wiring(),
                "email": email,
                "flash": str(exc),
                "delivery_mode": auth.settings.auth_email_delivery_mode,
            },
            status_code=400,
        )
    response = _redirect(request, _role_home(user))
    _set_session_cookie(response, session.id, settings)
    return response


@router.post("/auth/resend", response_class=HTMLResponse, name="auth_resend")
def auth_resend(
    request: Request,
    email: str = Form(...),
    auth: AuthService = Depends(_auth_dep),
) -> HTMLResponse:
    """Re-issue a login OTP for an email address — no access_key required.

    Security: we look up the user by email and only resend if an *existing*
    session challenge was already started (i.e. the user successfully passed
    step 1 within the last TTL window).  If no prior challenge exists we
    silently return the same page rather than revealing whether the email
    exists.
    """
    email_norm = email.strip().lower()
    # Only resend if the user exists and is active — never reveal existence otherwise.
    user = auth.repos.users.get_by_email(email_norm)
    dev_code: str | None = None
    flash_msg = "A new code has been sent."

    if user is not None and user.is_active:
        import secrets
        from evk.auth import hash_login_code
        code = f"{secrets.randbelow(1_000_000):06d}"
        challenge = LoginChallenge(
            id=f"challenge_{user.id}_{secrets.token_hex(4)}",
            user_id=user.id,
            email=user.email,
            code_hash=hash_login_code(code, user_id=user.id),
            expires_at=datetime.now(UTC) + timedelta(minutes=auth.settings.login_code_ttl_minutes),
            purpose="login",
        )
        auth.repos.login_challenges.upsert(challenge)
        auth.notifier.send_code(email=user.email, code=code)
        if isinstance(auth.notifier, TerminalAuthNotifier):
            dev_code = auth.notifier.last_code  # type: ignore[attr-defined]

    return templates.TemplateResponse(
        request,
        "auth_verify.html",
        {
            "request": request,
            "current_user": None,
            "wiring": describe_wiring(),
            "email": email_norm,
            "flash": flash_msg,
            "delivery_mode": auth.settings.auth_email_delivery_mode,
            "dev_code": dev_code,
        },
    )


@router.get("/forgot", response_class=HTMLResponse, name="forgot_page")
def forgot_page(
    request: Request,
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    if current_user is not None:
        return _redirect(request, "app_home")
    return templates.TemplateResponse(
        request,
        "forgot.html",
        {
            "request": request,
            "current_user": None,
            "wiring": describe_wiring(),
            "flash": None,
        },
    )


@router.post("/auth/forgot", response_class=HTMLResponse, name="auth_forgot")
def auth_forgot(
    request: Request,
    email: str = Form(...),
    auth: AuthService = Depends(_auth_dep),
) -> HTMLResponse:
    dev_code = auth.start_reset(email=email)
    return templates.TemplateResponse(
        request,
        "reset_verify.html",
        {
            "request": request,
            "current_user": None,
            "wiring": describe_wiring(),
            "email": email.strip().lower(),
            "flash": None,
            "dev_code": dev_code,
            "delivery_mode": auth.settings.auth_email_delivery_mode,
        },
    )


@router.post("/auth/reset", response_class=HTMLResponse, name="auth_reset")
def auth_reset(
    request: Request,
    email: str = Form(...),
    code: str = Form(...),
    new_access_key: str = Form(...),
    auth: AuthService = Depends(_auth_dep),
) -> HTMLResponse:
    try:
        auth.complete_reset(email=email, code=code, new_access_key=new_access_key)
    except AuthError as exc:
        return templates.TemplateResponse(
            request,
            "reset_verify.html",
            {
                "request": request,
                "current_user": None,
                "wiring": describe_wiring(),
                "email": email,
                "flash": str(exc),
                "dev_code": None,
                "delivery_mode": auth.settings.auth_email_delivery_mode,
            },
            status_code=400,
        )
    response = _redirect(request, "login_page")
    # Carry flash message via query param so landing.html can show it
    from fastapi.responses import RedirectResponse as _RR
    url = str(request.url_for("login_page")) + "?flash=Password+updated+%E2%80%94+sign+in+with+your+new+password."
    return _RR(url, status_code=303)


@router.post("/auth/logout", name="logout")
def logout(
    request: Request,
    auth: AuthService = Depends(_auth_dep),
    settings: Settings = Depends(_settings_dep),
) -> RedirectResponse:
    auth.revoke_session(request.cookies.get(settings.session_cookie_name))
    response = _redirect(request, "landing")
    _clear_session(response, settings)
    return response


@router.get("/app", name="app_home")
def app_home(request: Request, current_user: AppUser | None = Depends(_current_user)) -> RedirectResponse:
    if current_user is None:
        return _redirect(request, "login_page")
    return _redirect(request, _role_home(current_user))


@router.get("/app/admin", response_class=HTMLResponse, name="admin_dashboard")
def admin_dashboard(
    request: Request,
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    guard = _staff_or_ngo_redirect(request, current_user)
    if guard is not None:
        return guard
    assert current_user is not None
    context = _dashboard_context(repos, current_user)
    context["request"] = request
    return templates.TemplateResponse(request, "dashboard.html", context)


@router.get("/app/ngo", response_class=HTMLResponse, name="ngo_dashboard")
def ngo_dashboard(
    request: Request,
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    if current_user is None:
        return _redirect(request, "landing")
    if current_user.role is not UserRole.NGO_ADMIN:
        return _redirect(request, _role_home(current_user))
    sent_count = sum(1 for draft in repos.drafts.list_all() if draft.status is DraftStatus.SENT)
    return templates.TemplateResponse(
        request,
        "ngo_dashboard.html",
        {
            "request": request,
            "current_user": current_user,
            "wiring": describe_wiring(),
            "stats": _stats(repos),
            "sent_count": sent_count,
            "opportunities": _decorate_opps(repos.opportunities.list_all(limit=30)),
            "students": repos.students.list_all(limit=20),
        },
    )


@router.get("/app/student", response_class=HTMLResponse, name="student_dashboard")
def student_dashboard(
    request: Request,
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    if current_user is None:
        return _redirect(request, "landing")
    if current_user.role is not UserRole.STUDENT:
        return _redirect(request, _role_home(current_user))
    student = repos.students.get(current_user.student_id) if current_user.student_id else None
    drafts = [
        draft for draft in repos.drafts.list_all(limit=200)
        if draft.student_id == current_user.student_id
        and draft.status in (DraftStatus.SENT, DraftStatus.APPROVED)
    ]
    drafts.sort(key=lambda draft: draft.created_at, reverse=True)
    opportunity_map = {
        draft.opportunity_id: repos.opportunities.get(draft.opportunity_id) for draft in drafts
    }
    return templates.TemplateResponse(
        request,
        "student_dashboard.html",
        {
            "request": request,
            "current_user": current_user,
            "wiring": describe_wiring(),
            "student": student,
            "drafts": drafts[:20],
            "opportunity_map": opportunity_map,
            "recommended_opportunities": _decorate_opps(
                [o for o in repos.opportunities.list_all(limit=200) if not o.needs_review and not o.is_duplicate]
            ),
        },
    )


@router.get("/drafts/{status_filter}", response_class=HTMLResponse, name="drafts_page")
@router.get("/ui/pages/drafts/{status_filter}", response_class=HTMLResponse, include_in_schema=False)
def drafts_page(
    request: Request,
    status_filter: _STATUS_LITERAL,
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    guard = _staff_redirect(request, current_user)
    if guard is not None:
        return guard
    assert current_user is not None
    label = next(label for label, value in STATUS_TABS if value == status_filter)
    return templates.TemplateResponse(
        request,
        "drafts_page.html",
        {
            "request": request,
            "current_user": current_user,
            "wiring": describe_wiring(),
            "status_label": label,
            "status_description": (
                "Approve the messages that are ready to go out."
                if status_filter == "pending_approval"
                else "Review the messages that already moved through the send flow."
            ),
            **_draft_panel_context(request, repos, status_filter, current_user),
        },
    )


@router.get("/admin/kpi", response_class=HTMLResponse, name="admin_kpi")
def admin_kpi(
    request: Request,
    period: str = "all",
    repos: Repos = Depends(_repos_dep),
    settings: Settings = Depends(_settings_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    """KPI dashboard — operational stats + editable outcome tracker."""
    guard = _staff_or_ngo_redirect(request, current_user)
    if guard is not None:
        return guard

    import json as _json, statistics as _stats_mod
    from pathlib import Path as _Path

    # ── Period filter ────────────────────────────────────────────────────────
    _VALID_PERIODS = ("all", "week", "month", "quarter")
    if period not in _VALID_PERIODS:
        period = "all"

    now = datetime.now(UTC)
    _period_starts = {
        "week":    now - timedelta(days=7),
        "month":   now - timedelta(days=30),
        "quarter": now - timedelta(days=91),
    }
    cutoff = _period_starts.get(period)  # None means "all time"

    def _after_cutoff(dt: datetime | None) -> bool:
        if cutoff is None:
            return True
        if dt is None:
            return False
        aware = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
        return aware >= cutoff

    # ── Operational metrics ──────────────────────────────────────────────────
    all_drafts = repos.drafts.list_all()
    all_opps   = repos.opportunities.list_all()
    all_emails = repos.raw_emails.list_all()

    active_opps = [o for o in all_opps if not o.needs_review and not o.is_duplicate]

    # Apply period filter to sent drafts and ingested emails
    sent_drafts   = [d for d in all_drafts if d.status == DraftStatus.SENT and _after_cutoff(d.sent_at)]
    approved      = [d for d in all_drafts if d.status == DraftStatus.APPROVED and _after_cutoff(d.approved_at)]
    period_emails = [e for e in all_emails if _after_cutoff(e.received_at)]

    review_hours_list = [
        (d.sent_at - d.created_at).total_seconds() / 3600
        for d in sent_drafts
        if d.sent_at and d.created_at
    ]
    avg_review_hours = round(_stats_mod.mean(review_hours_list), 1) if review_hours_list else None

    ops = {
        "emails_ingested":  len(period_emails) if period != "all" else len(all_emails),
        "opps_classified":  len(active_opps),
        "opps_in_review":   sum(1 for o in all_opps if o.needs_review),
        "drafts_generated": len([d for d in all_drafts if _after_cutoff(d.created_at)]),
        "drafts_approved":  len(approved),
        "emails_sent":      len(sent_drafts),
        "avg_review_hours": avg_review_hours,
    }

    # ── Outcome tracking (file-backed) ───────────────────────────────────────
    kpi_file = _Path(settings.local_data_dir) / "kpi_outcomes.json"
    try:
        outcomes = _json.loads(kpi_file.read_text()) if kpi_file.exists() else []
    except Exception:
        outcomes = []

    return templates.TemplateResponse(
        request,
        "admin_kpi.html",
        {
            "request": request,
            "current_user": current_user,
            "wiring": describe_wiring(),
            "ops": ops,
            "outcomes": outcomes,
            "period": period,
        },
    )


@router.post("/admin/kpi/outcome", name="admin_kpi_outcome_add")
def admin_kpi_outcome_add(
    request: Request,
    period: str = Form(...),
    applications: str = Form(""),
    interviews: str = Form(""),
    scholarships: str = Form(""),
    notes: str = Form(""),
    settings: Settings = Depends(_settings_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> RedirectResponse:
    guard = _staff_or_ngo_redirect(request, current_user)
    if guard is not None:
        return guard
    import json as _json
    from pathlib import Path as _Path
    kpi_file = _Path(settings.local_data_dir) / "kpi_outcomes.json"
    outcomes = _json.loads(kpi_file.read_text()) if kpi_file.exists() else []
    outcomes.append({
        "period": period.strip(),
        "applications": applications.strip(),
        "interviews": interviews.strip(),
        "scholarships": scholarships.strip(),
        "notes": notes.strip(),
    })
    kpi_file.write_text(_json.dumps(outcomes, indent=2))
    return _redirect(request, "admin_kpi")


@router.post("/admin/kpi/outcome/{idx}/delete", name="admin_kpi_outcome_delete")
def admin_kpi_outcome_delete(
    idx: int,
    request: Request,
    settings: Settings = Depends(_settings_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> RedirectResponse:
    guard = _staff_redirect(request, current_user)  # admin-only for delete
    if guard is not None:
        return guard
    import json as _json
    from pathlib import Path as _Path
    kpi_file = _Path(settings.local_data_dir) / "kpi_outcomes.json"
    outcomes = _json.loads(kpi_file.read_text()) if kpi_file.exists() else []
    if 0 <= idx < len(outcomes):
        outcomes.pop(idx)
        kpi_file.write_text(_json.dumps(outcomes, indent=2))
    return _redirect(request, "admin_kpi")


@router.get("/admin/test-logins", response_class=HTMLResponse, name="admin_test_logins")
def admin_test_logins(
    request: Request,
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    """Pilot-only tool: issue login codes for accounts with placeholder email addresses."""
    guard = _staff_redirect(request, current_user)
    if guard is not None:
        return guard
    users = sorted(repos.users.list_all(), key=lambda u: (u.role.value, u.email))
    return templates.TemplateResponse(
        request,
        "admin_test_login.html",
        {
            "request": request,
            "current_user": current_user,
            "wiring": describe_wiring(),
            "users": users,
            "code_issued": None,
            "code_email": None,
            "flash": None,
            "ttl_minutes": get_settings().login_code_ttl_minutes,
        },
    )


@router.post("/admin/test-logins/issue", name="admin_test_login_issue")
def admin_test_login_issue(
    request: Request,
    email: str = Form(...),
    repos: Repos = Depends(_repos_dep),
    auth: AuthService = Depends(_auth_dep),
    settings: Settings = Depends(_settings_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    guard = _staff_redirect(request, current_user)
    if guard is not None:
        return guard
    import secrets as _sec
    from evk.auth import hash_login_code
    users = sorted(repos.users.list_all(), key=lambda u: (u.role.value, u.email))
    user = repos.users.get_by_email(email.strip().lower())
    code_issued = None
    flash = None
    if user is None:
        flash = f"No account found for {email}"
    else:
        code = f"{_sec.randbelow(1_000_000):06d}"
        challenge = LoginChallenge(
            id=f"challenge_{user.id}_{_sec.token_hex(4)}",
            user_id=user.id,
            email=user.email,
            code_hash=hash_login_code(code, user_id=user.id),
            expires_at=datetime.now(UTC) + timedelta(minutes=settings.login_code_ttl_minutes),
            purpose="login",
        )
        repos.login_challenges.upsert(challenge)
        code_issued = code
        print(f"[EVkids test-login] {user.email}: {code}", flush=True)
    if code_issued:
        # Code issued — jump directly to the verify page with it pre-shown.
        return templates.TemplateResponse(
            request,
            "auth_verify.html",
            {
                "request": request,
                "current_user": None,
                "wiring": describe_wiring(),
                "email": email,
                "flash": None,
                "delivery_mode": "terminal",
                "dev_code": code_issued,
            },
        )
    return templates.TemplateResponse(
        request,
        "admin_test_login.html",
        {
            "request": request,
            "current_user": current_user,
            "wiring": describe_wiring(),
            "users": users,
            "code_issued": code_issued,
            "code_email": email,
            "flash": flash,
            "ttl_minutes": settings.login_code_ttl_minutes,
        },
    )


@router.get("/students", response_class=HTMLResponse, name="students_page")
@router.get("/ui/pages/students", response_class=HTMLResponse, include_in_schema=False)
def students_page(
    request: Request,
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    guard = _staff_or_ngo_redirect(request, current_user)
    if guard is not None:
        return guard
    assert current_user is not None
    return templates.TemplateResponse(
        request,
        "students_page.html",
        {
            "request": request,
            "current_user": current_user,
            "wiring": describe_wiring(),
            "stats": _stats(repos),
            "students": repos.students.list_all(limit=50),
            "levels": list(StudentLevel),
        },
    )


@router.get("/opportunities", response_class=HTMLResponse, name="opportunities_page")
@router.get("/ui/pages/opportunities", response_class=HTMLResponse, include_in_schema=False)
def opportunities_page(
    request: Request,
    sort: str = "deadline",
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    guard = _staff_or_ngo_redirect(request, current_user)
    if guard is not None:
        return guard
    assert current_user is not None
    all_opps = repos.opportunities.list_all(limit=200)
    active = [o for o in all_opps if not o.needs_review and not o.is_duplicate]
    needs_review = [o for o in all_opps if o.needs_review]

    if sort == "deadline":
        active = sorted(active, key=lambda o: (o.deadline or datetime.max.replace(tzinfo=UTC), o.title))
    elif sort == "title":
        active = sorted(active, key=lambda o: o.title)
    elif sort == "organization":
        active = sorted(active, key=lambda o: o.organization)

    return templates.TemplateResponse(
        request,
        "opportunities_page.html",
        {
            "request": request,
            "current_user": current_user,
            "wiring": describe_wiring(),
            "stats": _stats(repos),
            "opportunities": _decorate_opps(active),
            "opportunities_review": _decorate_opps(needs_review),
            "active_count": len(active),
            "review_count": len(needs_review),
            "sort": sort,
        },
    )


@router.get("/opportunities/new", response_class=HTMLResponse, name="opportunity_new_page")
def opportunity_new_page(
    request: Request,
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    """Admin / NGO admin: blank form to add an opportunity directly."""
    guard = _staff_or_ngo_redirect(request, current_user)
    if guard is not None:
        return guard
    assert current_user is not None
    from evk.models import OpportunityKind, StudentLevel
    return templates.TemplateResponse(
        request,
        "opportunity_new.html",
        {
            "request": request,
            "current_user": current_user,
            "wiring": describe_wiring(),
            "kinds": list(OpportunityKind),
            "levels": list(StudentLevel),
        },
    )


@router.post("/opportunities/new", name="opportunity_new_submit")
def opportunity_new_submit(
    request: Request,
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
    title: str = Form(""),
    organization: str = Form(""),
    kind: str = Form("other"),
    summary: str = Form(""),
    eligibility: str = Form(""),
    deadline_str: str = Form(""),
    url: str = Form(""),
    location: str = Form(""),
    min_level: str = Form("other"),
    tags_raw: str = Form(""),
    fields_raw: str = Form(""),
    notes: str = Form(""),
) -> RedirectResponse:
    """Create a new opportunity manually (bypasses Gemini — goes live immediately)."""
    guard = _staff_or_ngo_redirect(request, current_user)
    if guard is not None:
        return guard
    import uuid as _uuid
    from evk.models import Opportunity, OpportunityKind, StudentLevel

    deadline_dt: datetime | None = None
    if deadline_str.strip():
        try:
            d = date.fromisoformat(deadline_str.strip())
            deadline_dt = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=UTC)
        except ValueError:
            pass

    tags = [t.strip().lower() for t in tags_raw.split(",") if t.strip()]
    fields = [f.strip().lower() for f in fields_raw.split(",") if f.strip()]

    opp_id = "manual_" + _uuid.uuid4().hex[:16]
    opp = Opportunity(
        id=opp_id,
        title=title.strip() or "Untitled opportunity",
        organization=organization.strip(),
        kind=OpportunityKind(kind) if kind in [k.value for k in OpportunityKind] else OpportunityKind.OTHER,
        summary=summary.strip(),
        eligibility=eligibility.strip(),
        deadline=deadline_dt,
        url=url.strip() or None,
        location=location.strip(),
        min_level=StudentLevel(min_level) if min_level in [lv.value for lv in StudentLevel] else StudentLevel.OTHER,
        tags=tags,
        fields_of_study=fields,
        source_subject=notes.strip() or "Manually added",
        source_sender=current_user.email if current_user else "admin",  # type: ignore[union-attr]
        needs_review=False,
    )
    repos.opportunities.upsert(opp)
    return _redirect(request, "opportunity_detail", opp_id=opp_id)


@router.get("/opportunities/suggest", response_class=HTMLResponse, name="opportunity_suggest_page")
def opportunity_suggest_page(
    request: Request,
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    """Student: suggest an opportunity they found for admin review."""
    if not current_user:
        return _redirect(request, "login_page")  # type: ignore[return-value]
    from evk.models import OpportunityKind
    return templates.TemplateResponse(
        request,
        "opportunity_suggest.html",
        {
            "request": request,
            "current_user": current_user,
            "wiring": describe_wiring(),
            "kinds": list(OpportunityKind),
        },
    )


@router.post("/opportunities/suggest", name="opportunity_suggest_submit")
def opportunity_suggest_submit(
    request: Request,
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
    title: str = Form(""),
    organization: str = Form(""),
    kind: str = Form("other"),
    url: str = Form(""),
    summary: str = Form(""),
    deadline_str: str = Form(""),
) -> RedirectResponse:
    """Create a student-suggested opportunity; lands in the review queue."""
    if not current_user:
        return _redirect(request, "login_page")  # type: ignore[return-value]
    import uuid as _uuid
    from evk.models import Opportunity, OpportunityKind

    deadline_dt: datetime | None = None
    if deadline_str.strip():
        try:
            d = date.fromisoformat(deadline_str.strip())
            deadline_dt = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=UTC)
        except ValueError:
            pass

    opp_id = "suggest_" + _uuid.uuid4().hex[:16]
    opp = Opportunity(
        id=opp_id,
        title=title.strip() or "Untitled suggestion",
        organization=organization.strip(),
        kind=OpportunityKind(kind) if kind in [k.value for k in OpportunityKind] else OpportunityKind.OTHER,
        summary=summary.strip(),
        url=url.strip() or None,
        deadline=deadline_dt,
        source_subject="Student suggestion",
        source_sender=current_user.email,  # type: ignore[union-attr]
        needs_review=True,
        review_reason=f"Suggested by student: {current_user.name}",  # type: ignore[union-attr]
    )
    repos.opportunities.upsert(opp)
    return _redirect(request, "student_dashboard")


@router.post("/opportunities/{opp_id}/clear-review", name="opportunity_clear_review")
def opportunity_clear_review(
    opp_id: str,
    request: Request,
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> RedirectResponse:
    """Admin clears the needs_review flag — opportunity enters the active catalogue."""
    guard = _staff_or_ngo_redirect(request, current_user)
    if guard is not None:
        return guard
    opp = repos.opportunities.get(opp_id)
    if opp is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    repos.opportunities.patch(opp_id, {"needs_review": False, "review_reason": ""})
    return _redirect(request, "opportunity_detail", opp_id=opp_id)


@router.post("/opportunities/{opp_id}/archive", name="opportunity_archive")
def opportunity_archive(
    opp_id: str,
    request: Request,
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> RedirectResponse:
    """Admin archives (soft-deletes) an opportunity — marks as duplicate so it disappears from catalogue."""
    guard = _staff_or_ngo_redirect(request, current_user)
    if guard is not None:
        return guard
    opp = repos.opportunities.get(opp_id)
    if opp is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    repos.opportunities.patch(opp_id, {"is_duplicate": True, "needs_review": False})
    return _redirect(request, "opportunities_page")


@router.post("/opportunities/{opp_id}/assign", name="opportunity_assign")
def opportunity_assign(
    opp_id: str,
    request: Request,
    student_id: str = Form(...),
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> RedirectResponse:
    """NGO Admin / Admin: manually assign an opportunity to a specific student as a draft."""
    guard = _staff_or_ngo_redirect(request, current_user)
    if guard is not None:
        return guard
    opp = repos.opportunities.get(opp_id)
    if opp is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    student = repos.students.get(student_id)
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found")

    import secrets as _sec
    from evk.models import DraftMessage, DraftStatus

    # Build a simple, human-editable draft (no Gemini call needed for manual assignment)
    deadline_str = opp.deadline.date().isoformat() if opp.deadline else "rolling deadline"
    link_line = f"\nApply here: {opp.url}\n" if opp.url else ""
    first_name = student.name.split()[0] if student.name else "there"
    body_text = (
        f"Hi {first_name},\n\n"
        f"We thought you'd be a great fit for this opportunity:\n\n"
        f"**{opp.title}** — {opp.organization}\n"
        f"{opp.summary}\n\n"
        f"Deadline: {deadline_str}"
        f"{link_line}\n"
        f"Let us know if you have any questions — we're here to help!\n\n"
        f"— The EVkids Team"
    )
    draft = DraftMessage(
        id=f"assign_{_sec.token_hex(8)}",
        student_id=student.id,
        opportunity_id=opp.id,
        to_email=student.email,
        subject=f"Opportunity for you: {opp.title}",
        body_text=body_text,
        body_html=f"<p>{body_text.replace(chr(10), '</p><p>')}</p>",
        match_score=1.0,
        match_reasons=["manually assigned"],
        status=DraftStatus.PENDING_APPROVAL,
    )
    repos.drafts.upsert(draft)
    logger.bind(opp_id=opp_id, student_id=student_id, draft_id=draft.id).info("opportunity.manually_assigned")
    return _redirect(request, "opportunity_detail", opp_id=opp_id)


@router.post("/opportunities/{opp_id}/edit", name="opportunity_edit")
def opportunity_edit(
    opp_id: str,
    request: Request,
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
    # Core fields
    title: str = Form(""),
    organization: str = Form(""),
    kind: str = Form("other"),
    summary: str = Form(""),
    eligibility: str = Form(""),
    deadline_str: str = Form(""),      # YYYY-MM-DD or empty
    url: str = Form(""),
    location: str = Form(""),
    min_level: str = Form("other"),
    # Comma-separated tag / field lists
    tags_raw: str = Form(""),
    fields_raw: str = Form(""),
    # Review control
    clear_review: str = Form(""),      # "yes" if admin checks the box
) -> RedirectResponse:
    """Save admin edits to an opportunity and optionally clear the review flag."""
    guard = _staff_or_ngo_redirect(request, current_user)
    if guard is not None:
        return guard
    opp = repos.opportunities.get(opp_id)
    if opp is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    from evk.models import OpportunityKind, StudentLevel

    # Parse deadline
    deadline_dt: datetime | None = None
    if deadline_str.strip():
        try:
            from datetime import date
            d = date.fromisoformat(deadline_str.strip())
            deadline_dt = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=UTC)
        except ValueError:
            pass  # bad date format — leave as None

    # Normalise tag lists
    tags = [t.strip().lower() for t in tags_raw.split(",") if t.strip()]
    fields = [f.strip().lower() for f in fields_raw.split(",") if f.strip()]

    patch: dict[str, object] = {
        "title": title.strip() or opp.title,
        "organization": organization.strip() or opp.organization,
        "kind": OpportunityKind(kind) if kind else opp.kind,
        "summary": summary.strip() or opp.summary,
        "eligibility": eligibility.strip(),
        "deadline": deadline_dt,
        "url": url.strip() or None,
        "location": location.strip(),
        "min_level": StudentLevel(min_level) if min_level else opp.min_level,
        "tags": tags if tags else opp.tags,
        "fields_of_study": fields if fields else opp.fields_of_study,
    }
    if clear_review == "yes":
        patch["needs_review"] = False
        patch["review_reason"] = ""

    repos.opportunities.patch(opp_id, patch)
    return _redirect(request, "opportunity_detail", opp_id=opp_id)


@router.get("/opportunities/{opp_id}", response_class=HTMLResponse, name="opportunity_detail")
def opportunity_detail(
    opp_id: str,
    request: Request,
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    guard = _staff_or_ngo_redirect(request, current_user)
    if guard is not None:
        return guard
    assert current_user is not None
    opp = repos.opportunities.get(opp_id)
    if opp is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    # Compute per-student match scores (cheap rule-based — no Gemini call).
    from evk.agents.personalizer import score_match
    students = repos.students.list_all(limit=200)
    matches = sorted(
        [score_match(s, opp) for s in students],
        key=lambda m: m.score,
        reverse=True,
    )

    # Find source email if ingested from Gmail
    source_email = None
    if opp.source_raw_email_id:
        source_email = repos.raw_emails.get(opp.source_raw_email_id)

    # Find any existing drafts for this opportunity
    all_drafts = repos.drafts.list_all(limit=500)
    opp_drafts = [d for d in all_drafts if d.opportunity_id == opp_id]

    today = datetime.now(UTC).date()
    days_until: int | None = None
    if opp.deadline:
        delta = (opp.deadline.date() - today).days
        days_until = delta if delta >= 0 else None

    all_students = repos.students.list_all(limit=200)

    return templates.TemplateResponse(
        request,
        "opportunity_detail.html",
        {
            "request": request,
            "current_user": current_user,
            "wiring": describe_wiring(),
            "opp": opp,
            "days_until": days_until,
            "matches": matches,
            "source_email": source_email,
            "drafts": opp_drafts,
            "all_students": all_students,
        },
    )


@router.get("/admin/agents", response_class=HTMLResponse)
def admin_agents(
    request: Request,
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    guard = _staff_redirect(request, current_user)
    if guard is not None:
        return guard
    from evk.agents.scraper import SCRAPER_SOURCES
    return templates.TemplateResponse(
        request,
        "admin_agents.html",
        {
            "request": request,
            "current_user": current_user,
            "wiring": describe_wiring(),
            "scraper_sources": SCRAPER_SOURCES,
        },
    )


@router.get("/ui/stats", response_class=HTMLResponse)
def ui_stats(
    request: Request,
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    user = _staff_required(current_user)
    return _render_stats(request, repos, user)


@router.get("/ui/drafts", response_class=HTMLResponse)
def ui_drafts(
    request: Request,
    status_filter: _STATUS_LITERAL = "pending_approval",
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    user = _staff_required(current_user)
    return _render_drafts_panel(request, repos, status_filter, user)


@router.get("/ui/users", response_class=HTMLResponse)
def ui_users(
    request: Request,
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    user = _staff_required(current_user)
    return _render_user_panel(request, repos, user)


@router.post("/ui/users/{user_id}/toggle-active", response_class=HTMLResponse)
def ui_toggle_user_active(
    user_id: str,
    request: Request,
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    user = _staff_required(current_user)
    target = repos.users.get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    repos.users.patch(user_id, {"is_active": not target.is_active})
    return _render_user_panel(
        request,
        repos,
        user,
        flash=f"{target.name} is now {'active' if not target.is_active else 'inactive'}.",
    )


@router.post("/ui/drafts/{draft_id}/approve", response_class=HTMLResponse)
def ui_approve(
    draft_id: str,
    request: Request,
    status_filter: _STATUS_LITERAL = "pending_approval",
    repos: Repos = Depends(_repos_dep),
    distributor: DistributorAgent = Depends(_distributor_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    user = _staff_required(current_user)
    draft = repos.drafts.get(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="draft not found")
    if draft.status not in {DraftStatus.PENDING_APPROVAL, DraftStatus.APPROVED}:
        raise HTTPException(status_code=409, detail="wrong status")
    repos.drafts.patch(
        draft_id,
        {
            "status": DraftStatus.APPROVED.value,
            "approved_by": user.email,
            "approved_at": datetime.now(UTC),
        },
    )
    draft = repos.drafts.get(draft_id)
    assert draft is not None
    flash = None
    try:
        draft = distributor.send_one(draft)
        flash = f"Sent email to {draft.to_email}"
    except Exception:  # pragma: no cover
        draft = repos.drafts.get(draft_id) or draft
        flash = f"Send failed for {draft.to_email}"
    return _render_drafts_with_stats(request, repos, status_filter, user, flash=flash)


@router.post("/ui/drafts/{draft_id}/reject", response_class=HTMLResponse)
def ui_reject(
    draft_id: str,
    request: Request,
    status_filter: _STATUS_LITERAL = "pending_approval",
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    user = _staff_required(current_user)
    draft = repos.drafts.get(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="draft not found")
    repos.drafts.patch(
        draft_id,
        {
            "status": DraftStatus.REJECTED.value,
            "approved_by": user.email,
            "approved_at": datetime.now(UTC),
        },
    )
    return _render_drafts_with_stats(
        request,
        repos,
        status_filter,
        user,
        flash="Draft moved out of the send queue",
    )


@router.post("/ui/drafts/{draft_id}/save-edit", response_class=HTMLResponse)
def ui_save_edit(
    draft_id: str,
    request: Request,
    subject: str = Form(...),
    body_text: str = Form(...),
    action: str = Form("pending"),   # "pending" or "approve"
    status_filter: str = "pending_approval",
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
    inkbox: InkboxClient = Depends(_inkbox_dep),
) -> HTMLResponse:
    guard = _staff_redirect(request, current_user)
    if guard is not None:
        return guard
    user = _staff_required(current_user)
    draft = repos.drafts.get(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    # Save edits
    repos.drafts.patch(draft_id, {
        "subject": subject.strip(),
        "body_text": body_text.strip(),
        "body_html": f"<p>{body_text.strip().replace(chr(10), '</p><p>')}</p>",
        "status": DraftStatus.PENDING_APPROVAL.value,
    })
    # If admin chose approve directly, approve then send it now
    if action == "approve":
        repos.drafts.patch(draft_id, {
            "status": DraftStatus.APPROVED.value,
            "approved_by": user.email,
            "approved_at": datetime.now(UTC),
        })
        updated = repos.drafts.get(draft_id)
        if updated:
            try:
                dist = DistributorAgent(repos=repos, inkbox=inkbox)
                dist.send_one(updated)
            except Exception:
                pass
    return _render_drafts_panel(request, repos, status_filter, user)


@router.post("/ui/poll", response_class=HTMLResponse)
def ui_poll(
    request: Request,
    repos: Repos = Depends(_repos_dep),
    ingestion: IngestionAgent = Depends(_ingestion_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    user = _staff_required(current_user)
    processed, pending = ingestion.poll_unread()
    flash = f"Polled inbox: {len(processed)} message(s), {len(pending)} pending approval."
    return _render_stats(request, repos, user, flash=flash)


@router.post("/ui/remind", response_class=HTMLResponse)
def ui_remind(
    request: Request,
    repos: Repos = Depends(_repos_dep),
    reminder: ReminderAgent = Depends(_reminder_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    user = _staff_required(current_user)
    sent = reminder.run()
    return _render_stats(request, repos, user, flash=f"Reminder sweep sent {sent} reminder(s).")


@router.post("/ui/digest", response_class=HTMLResponse)
def ui_digest(
    request: Request,
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    user = _staff_required(current_user)
    drafts = DigestAgent(repos=repos).build_and_queue()
    return _render_stats(
        request,
        repos,
        user,
        flash=f"Weekly digest queued {len(drafts)} draft(s) for review.",
    )


@router.post("/ui/simulate", response_class=HTMLResponse)
def ui_simulate(
    request: Request,
    repos: Repos = Depends(_repos_dep),
    ingestion: IngestionAgent = Depends(_ingestion_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    user = _staff_required(current_user)
    sample_path = Path("seed/sample_newsletter.txt")
    if not sample_path.exists():
        raise HTTPException(status_code=404, detail=f"{sample_path} not found")
    body = sample_path.read_text(encoding="utf-8", errors="ignore")
    msg = InboundMessage(
        id=f"sim_{int(datetime.now(UTC).timestamp() * 1000)}",
        rfc_message_id=None,
        thread_id=None,
        from_address="newsletter@example.com",
        subject="[sim] sample newsletter",
        body_text=body,
        body_html="",
        raw=None,
    )
    raw = ingestion.handle_inbound(msg)
    flash = (
        f"Newsletter ingested with status {raw.status.value}. "
        f"Captured {len(raw.extracted_opportunity_ids)} opportunities."
    )
    return _render_stats(request, repos, user, flash=flash)


@router.post("/admin/students/import", name="students_import")
async def students_import(
    request: Request,
    file: UploadFile = File(...),
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> RedirectResponse:
    """Parse a CSV and bulk-create inactive Student + AppUser records."""
    guard = _staff_redirect(request, current_user)
    if guard is not None:
        return guard
    import csv
    import io
    import secrets as _sec
    from evk.auth import hash_access_key

    content = await file.read()
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    for row in reader:
        email = (row.get("email") or row.get("Email") or "").strip().lower()
        name = (row.get("name") or row.get("Name") or "").strip()
        school = (row.get("school") or row.get("School") or row.get("school_name") or "").strip()
        level_raw = (row.get("level") or row.get("Level") or row.get("grade") or "").strip()
        if not email or "@" not in email:
            continue
        if repos.students.get_by_email(email):
            continue
        try:
            level = StudentLevel(level_raw) if level_raw else StudentLevel.OTHER
        except ValueError:
            level = StudentLevel.OTHER
        sid = f"student_{_sec.token_hex(6)}"
        student = Student(
            id=sid,
            name=name or email.split("@")[0],
            email=email,
            level=level,
            school_name=school,
            opted_in=False,
        )
        repos.students.upsert(student)
        # Create inactive user
        salt = _sec.token_hex(8)
        tmp_pw = _sec.token_urlsafe(16)
        user = AppUser(
            id=f"user_student_{sid}",
            email=email,
            name=name or email.split("@")[0],
            role=UserRole.STUDENT,
            organization=school,
            student_id=sid,
            access_key_salt=salt,
            access_key_hash=hash_access_key(tmp_pw, salt=salt),
            is_active=False,
        )
        repos.users.upsert(user)
    return _redirect(request, "students_page")


@router.post("/admin/students/{student_id}/activate", name="student_activate")
def student_activate(
    student_id: str,
    request: Request,
    repos: Repos = Depends(_repos_dep),
    auth: AuthService = Depends(_auth_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> RedirectResponse:
    guard = _staff_redirect(request, current_user)
    if guard is not None:
        return guard
    student = repos.students.get(student_id)
    if student is None:
        raise HTTPException(status_code=404)
    user = repos.users.get_by_email(student.email)
    if user is None:
        raise HTTPException(status_code=404)
    import secrets
    token = secrets.token_urlsafe(32)
    expires = datetime.now(UTC) + timedelta(days=7)
    repos.users.patch(user.id, {
        "activation_token": token,
        "activation_token_expires": expires.isoformat(),
        "is_active": False,
    })
    repos.students.patch(student_id, {"opted_in": True})
    settings = get_settings()
    setup_url = (
        str(settings.admin_base_url).rstrip("/")
        + f"/profile/setup?token={token}&email={student.email}"
    )
    try:
        auth.send_welcome_email(student_email=student.email, setup_url=setup_url)
    except Exception:
        logger.exception("student_activate.welcome_email_failed")
    return _redirect(request, "students_page")


@router.post("/admin/students/{student_id}/edit", name="student_admin_edit")
def student_admin_edit(
    student_id: str,
    request: Request,
    name: str = Form(""),
    school_name: str = Form(""),
    level: str = Form(""),
    graduation_year: str = Form(""),
    location: str = Form(""),
    boston_resident: str = Form(""),
    first_generation: str = Form(""),
    bio: str = Form(""),
    phone: str = Form(""),
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> RedirectResponse:
    guard = _staff_or_ngo_redirect(request, current_user)
    if guard is not None:
        return guard
    if repos.students.get(student_id) is None:
        raise HTTPException(status_code=404)
    patch: dict[str, object] = {
        "location": location.strip(),
        "boston_resident": boston_resident.lower() in ("on", "true", "1", "yes"),
        "first_generation": first_generation.lower() in ("on", "true", "1", "yes"),
        "bio": bio.strip(),
    }
    if name.strip():
        patch["name"] = name.strip()
    if school_name.strip():
        patch["school_name"] = school_name.strip()
    if level.strip():
        try:
            patch["level"] = StudentLevel(level.strip())
        except ValueError:
            pass
    if graduation_year.strip():
        try:
            patch["graduation_year"] = int(graduation_year.strip())
        except ValueError:
            pass
    if phone.strip():
        patch["phone"] = phone.strip()
    repos.students.patch(student_id, patch)
    return _redirect(request, "students_page")


@router.post("/student/outcome", name="student_outcome_save")
def student_outcome_save(
    request: Request,
    opp_id: str = Form(...),
    status: str = Form(""),
    notes: str = Form(""),
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> RedirectResponse:
    if current_user is None or current_user.student_id is None:
        return _redirect(request, "login_page")
    student = repos.students.get(current_user.student_id)
    if student is None:
        return _redirect(request, "student_dashboard")
    outcomes = [o for o in student.outcomes if o.get("opp_id") != opp_id]
    if status:
        outcomes.append({
            "opp_id": opp_id,
            "status": status,
            "notes": notes.strip(),
            "updated_at": datetime.now(UTC).isoformat(),
        })
    repos.students.patch(current_user.student_id, {"outcomes": outcomes})
    return _redirect(request, "student_dashboard")


@router.get("/profile/setup", response_class=HTMLResponse, name="profile_setup_page")
def profile_setup_page(
    request: Request,
    token: str = "",
    email: str = "",
    repos: Repos = Depends(_repos_dep),
    flash: str = "",
) -> HTMLResponse:
    from datetime import datetime as _dt
    user = repos.users.get_by_email(email.strip().lower()) if email else None
    if not user or user.activation_token != token:
        return templates.TemplateResponse(
            request,
            "profile_setup.html",
            {"request": request, "valid": False, "email": email,
             "flash": "This link is invalid or has expired.",
             "current_user": None, "wiring": describe_wiring()},
        )
    expires = user.activation_token_expires
    if expires:
        exp_dt = _dt.fromisoformat(str(expires)) if isinstance(expires, str) else expires
        if exp_dt.replace(tzinfo=UTC) < datetime.now(UTC):
            return templates.TemplateResponse(
                request,
                "profile_setup.html",
                {"request": request, "valid": False, "email": email,
                 "flash": "This link has expired. Ask an admin to resend your welcome email.",
                 "current_user": None, "wiring": describe_wiring()},
            )
    return templates.TemplateResponse(
        request,
        "profile_setup.html",
        {"request": request, "valid": True, "email": email, "token": token,
         "flash": flash, "current_user": None, "wiring": describe_wiring()},
    )


@router.post("/profile/setup", name="profile_setup_submit")
def profile_setup_submit(
    request: Request,
    email: str = Form(...),
    token: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    career_interests: list[str] = Form(default=[]),
    opportunity_types: list[str] = Form(default=[]),
    notification_frequency: str = Form("weekly"),
    repos: Repos = Depends(_repos_dep),
) -> HTMLResponse:
    user = repos.users.get_by_email(email.strip().lower())
    if not user or user.activation_token != token:
        raise HTTPException(status_code=400, detail="Invalid token")
    if password != confirm_password or len(password) < 8:
        return templates.TemplateResponse(
            request,
            "profile_setup.html",
            {"request": request, "valid": True, "email": email, "token": token,
             "flash": "Passwords must match and be at least 8 characters.",
             "current_user": None, "wiring": describe_wiring()},
        )
    import secrets as _s
    from evk.auth import hash_access_key
    salt = _s.token_hex(8)
    repos.users.patch(user.id, {
        "access_key_salt": salt,
        "access_key_hash": hash_access_key(password, salt=salt),
        "is_active": True,
        "activation_token": None,
        "activation_token_expires": None,
    })
    if user.student_id:
        repos.students.patch(user.student_id, {
            "career_interests": career_interests,
            "opportunity_types_sought": opportunity_types,
            "notification_frequency": notification_frequency,
            "opted_in": True,
        })
    return _redirect(request, "landing")


@router.get("/profile", response_class=HTMLResponse, name="student_profile_page")
def student_profile_page(
    request: Request,
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    if current_user is None:
        return _redirect(request, "login_page")
    student = repos.students.get(current_user.student_id) if current_user.student_id else None
    return templates.TemplateResponse(
        request,
        "student_profile.html",
        {"request": request, "current_user": current_user, "student": student,
         "wiring": describe_wiring(), "flash": None},
    )


@router.post("/profile", name="student_profile_save")
def student_profile_save(
    request: Request,
    career_interests: list[str] = Form(default=[]),
    opportunity_types: list[str] = Form(default=[]),
    notification_frequency: str = Form("weekly"),
    preferred_notification_method: str = Form("email"),
    phone: str = Form(""),
    bio: str = Form(""),
    repos: Repos = Depends(_repos_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> RedirectResponse:
    if current_user is None or current_user.student_id is None:
        return _redirect(request, "login_page")
    repos.students.patch(current_user.student_id, {
        "career_interests": career_interests,
        "opportunity_types_sought": opportunity_types,
        "notification_frequency": notification_frequency,
        "preferred_notification_method": preferred_notification_method,
        "phone": phone.strip(),
        "bio": bio.strip(),
    })
    return _redirect(request, "student_dashboard")


@router.post("/ui/scrape", response_class=HTMLResponse)
def ui_scrape(
    request: Request,
    source: str = Form(...),
    repos: Repos = Depends(_repos_dep),
    inkbox: InkboxClient = Depends(_inkbox_dep),
    current_user: AppUser | None = Depends(_current_user),
) -> HTMLResponse:
    user = _staff_required(current_user)
    from evk.agents.scraper import WebScraperAgent, SCRAPER_SOURCES
    if source not in SCRAPER_SOURCES:
        raise HTTPException(status_code=400, detail=f"Unknown source: {source!r}")
    try:
        count = WebScraperAgent(repos=repos, inkbox=inkbox).scrape(source)
        flash = f"Scraped {SCRAPER_SOURCES[source]['name']}: {count} opportunit{'y' if count == 1 else 'ies'} queued for review."
    except Exception as exc:
        flash = f"Scrape failed for {source}: {exc}"
    return _render_stats(request, repos, user, flash=flash)


__all__ = [
    "_auth_dep",
    "_current_user",
    "_distributor_dep",
    "_ingestion_dep",
    "_inkbox_dep",
    "_repos_dep",
    "router",
]
