"""Admin/NGO dashboards, drafts, opportunities, HTMX fragments."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from evk.agents.digest import DigestAgent
from evk.agents.distributor import DistributorAgent
from evk.agents.ingestion import IngestionAgent
from evk.agents.reminder import ReminderAgent
from evk.auth import AuthError, AuthService, TerminalAuthNotifier, build_auth_notifier
from evk.config import Settings, get_settings
from evk.factory import describe_wiring, get_inkbox, get_repos
from evk.firestore_repo import Repos
from evk.inkbox_client import InboundMessage, InkboxClient
from evk.logging import logger
from evk.models import AppUser, DraftMessage, DraftStatus, LoginChallenge, Opportunity, Student, StudentLevel, UserRole
from evk.ui.deps import (
    auth_dep,
    current_user,
    distributor_dep,
    ingestion_dep,
    redirect,
    reminder_dep,
    repos_dep,
    settings_dep,
    set_session_cookie,
    staff_or_ngo_required,
    staff_required,
)
from evk.ui.helpers import allow_auth_resend, flash_redirect, parse_deadline_form, recommend_for_student
from evk.ui.template_env import templates
from evk.ui.view_models import (
    STATUS_TABS,
    _STATUS_LITERAL,
    auth_page_context,
    dashboard_context,
    decorate_opps,
    draft_panel_context,
    render_drafts_panel,
    render_drafts_with_stats,
    render_stats,
    render_user_panel,
    role_home,
    staff_or_ngo_redirect,
    staff_redirect,
    stats as _stats,
)

router = APIRouter(tags=["ui-admin"])

@router.get("/app", name="app_home")
def app_home(request: Request, current_user: AppUser | None = Depends(current_user)) -> RedirectResponse:
    if current_user is None:
        return redirect(request, "login_page")
    return redirect(request, role_home(current_user))


@router.get("/app/admin", response_class=HTMLResponse, name="admin_dashboard")
def admin_dashboard(
    request: Request,
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
) -> HTMLResponse:
    guard = staff_or_ngo_redirect(request, current_user)
    if guard is not None:
        return guard
    assert current_user is not None
    context = dashboard_context(repos, current_user)
    context["request"] = request
    return templates.TemplateResponse(request, "dashboard.html", context)


@router.get("/app/ngo", response_class=HTMLResponse, name="ngo_dashboard")
def ngo_dashboard(
    request: Request,
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
) -> HTMLResponse:
    if current_user is None:
        return redirect(request, "landing")
    if current_user.role is not UserRole.NGO_ADMIN:
        return redirect(request, role_home(current_user))
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
            "opportunities": decorate_opps(repos.opportunities.list_all(limit=30)),
            "students": repos.students.list_all(limit=20),
            "levels": list(StudentLevel),
        },
    )


@router.get("/drafts/{status_filter}", response_class=HTMLResponse, name="drafts_page")
@router.get("/ui/pages/drafts/{status_filter}", response_class=HTMLResponse, include_in_schema=False)
def drafts_page(
    request: Request,
    status_filter: _STATUS_LITERAL,
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
) -> HTMLResponse:
    guard = staff_or_ngo_redirect(request, current_user)
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
            **draft_panel_context(request, repos, status_filter, current_user),
        },
    )


@router.get("/admin/kpi", response_class=HTMLResponse, name="admin_kpi")
def admin_kpi(
    request: Request,
    period: str = "all",
    repos: Repos = Depends(repos_dep),
    settings: Settings = Depends(settings_dep),
    current_user: AppUser | None = Depends(current_user),
) -> HTMLResponse:
    """KPI dashboard — operational stats + editable outcome tracker."""
    guard = staff_or_ngo_redirect(request, current_user)
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

    # Students reached = distinct students who actually received a sent email.
    # "Students matched" is the backend signal; "emails sent" is the surfaced count.
    students_reached = len({d.student_id for d in sent_drafts})

    # ── Live student outcome states (Bug 14 & 15) ─────────────────────────
    all_students = repos.students.list_all()
    student_states: dict[str, int] = {
        "active_pending": 0,   # have applied/interested outcomes still open
        "progressed": 0,       # interview or next step
        "awarded": 0,          # won / accepted
        "closed": 0,           # passed / rejected
    }
    for st in all_students:
        for outcome in st.outcomes:
            status = outcome.get("status", "")
            if status in ("interested", "applied"):
                student_states["active_pending"] += 1
            elif status == "interview":
                student_states["progressed"] += 1
            elif status == "won":
                student_states["awarded"] += 1
            elif status in ("passed", "rejected"):
                student_states["closed"] += 1

    ops = {
        "emails_ingested":  len(period_emails) if period != "all" else len(all_emails),
        "opps_classified":  len(active_opps),
        "opps_in_review":   sum(1 for o in all_opps if o.needs_review),
        "drafts_generated": len([d for d in all_drafts if _after_cutoff(d.created_at)]),
        "emails_sent":      len(sent_drafts),
        "students_reached": students_reached,
        "avg_review_hours": avg_review_hours,
        "student_states":   student_states,
        "total_students":   len(all_students),
    }

    # ── Outcome tracking (file-backed) ───────────────────────────────────────
    kpi_file = _Path(settings.local_data_dir) / "kpi_outcomes.json"
    try:
        outcomes = _json.loads(kpi_file.read_text()) if kpi_file.exists() else []
    except Exception:
        outcomes = []

    # ── Per-student live outcomes for admin management ─────────────────────
    student_outcomes = []
    opp_map = {o.id: o for o in all_opps}
    for st in all_students:
        for outcome in st.outcomes:
            opp = opp_map.get(outcome.get("opp_id", ""))
            student_outcomes.append({
                "student_id": st.id,
                "student_name": st.name,
                "opp_id": outcome.get("opp_id", ""),
                "opp_title": opp.title if opp else "Unknown",
                "status": outcome.get("status", ""),
                "updated_at": outcome.get("updated_at", ""),
            })

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
            "student_outcomes": student_outcomes,
            "all_students": all_students,
            "all_opps": [o for o in all_opps if not o.is_duplicate],
        },
    )


@router.post("/admin/kpi/outcome", name="admin_kpi_outcome_add")
def admin_kpi_outcome_add(
    request: Request,
    period: str = Form(...),
    applications: str = Form(""),
    progressed: str = Form(""),
    awarded: str = Form(""),
    rejected: str = Form(""),
    settings: Settings = Depends(settings_dep),
    current_user: AppUser | None = Depends(current_user),
) -> RedirectResponse:
    guard = staff_or_ngo_redirect(request, current_user)
    if guard is not None:
        return guard
    import json as _json
    from pathlib import Path as _Path
    kpi_file = _Path(settings.local_data_dir) / "kpi_outcomes.json"
    outcomes = _json.loads(kpi_file.read_text()) if kpi_file.exists() else []
    outcomes.append({
        "period": period.strip(),
        "applications": applications.strip(),
        "progressed": progressed.strip(),
        "awarded": awarded.strip(),
        "rejected": rejected.strip(),
    })
    kpi_file.write_text(_json.dumps(outcomes, indent=2))
    return redirect(request, "admin_kpi")


@router.post("/admin/kpi/outcome/{idx}/delete", name="admin_kpi_outcome_delete")
def admin_kpi_outcome_delete(
    idx: int,
    request: Request,
    settings: Settings = Depends(settings_dep),
    current_user: AppUser | None = Depends(current_user),
) -> RedirectResponse:
    guard = staff_redirect(request, current_user)  # admin-only for delete
    if guard is not None:
        return guard
    import json as _json
    from pathlib import Path as _Path
    kpi_file = _Path(settings.local_data_dir) / "kpi_outcomes.json"
    outcomes = _json.loads(kpi_file.read_text()) if kpi_file.exists() else []
    if 0 <= idx < len(outcomes):
        outcomes.pop(idx)
        kpi_file.write_text(_json.dumps(outcomes, indent=2))
    return redirect(request, "admin_kpi")


@router.post("/admin/student-outcome", name="admin_student_outcome_save")
def admin_student_outcome_save(
    request: Request,
    student_id: str = Form(...),
    opp_id: str = Form(...),
    status: str = Form(""),
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
) -> RedirectResponse:
    guard = staff_or_ngo_redirect(request, current_user)
    if guard is not None:
        return guard
    student = repos.students.get(student_id)
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found")
    outcomes = [o for o in student.outcomes if o.get("opp_id") != opp_id]
    if status:
        outcomes.append({
            "opp_id": opp_id,
            "status": status,
            "notes": f"Updated by {current_user.name}",
            "updated_at": datetime.now(UTC).isoformat(),
        })
    repos.students.patch(student_id, {"outcomes": outcomes})
    return redirect(request, "admin_kpi")


@router.get("/admin/test-logins", response_class=HTMLResponse, name="admin_test_logins")
def admin_test_logins(
    request: Request,
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
) -> HTMLResponse:
    """Pilot-only tool: issue login codes for accounts with placeholder email addresses."""
    guard = staff_redirect(request, current_user)
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
    repos: Repos = Depends(repos_dep),
    auth: AuthService = Depends(auth_dep),
    settings: Settings = Depends(settings_dep),
    current_user: AppUser | None = Depends(current_user),
) -> HTMLResponse:
    guard = staff_redirect(request, current_user)
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
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
) -> HTMLResponse:
    guard = staff_or_ngo_redirect(request, current_user)
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
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
) -> HTMLResponse:
    guard = staff_or_ngo_redirect(request, current_user)
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
            "opportunities": decorate_opps(active),
            "opportunities_review": decorate_opps(needs_review),
            "active_count": len(active),
            "review_count": len(needs_review),
            "sort": sort,
        },
    )


@router.get("/opportunities/new", response_class=HTMLResponse, name="opportunity_new_page")
def opportunity_new_page(
    request: Request,
    current_user: AppUser | None = Depends(current_user),
) -> HTMLResponse:
    """Admin / NGO admin: blank form to add an opportunity directly."""
    guard = staff_or_ngo_redirect(request, current_user)
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
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
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
    guard = staff_or_ngo_redirect(request, current_user)
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
    return redirect(request, "opportunity_detail", opp_id=opp_id)


@router.post("/opportunities/{opp_id}/clear-review", name="opportunity_clear_review")
def opportunity_clear_review(
    opp_id: str,
    request: Request,
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
) -> RedirectResponse:
    """Admin clears the needs_review flag — opportunity enters the active catalog."""
    guard = staff_or_ngo_redirect(request, current_user)
    if guard is not None:
        return guard
    opp = repos.opportunities.get(opp_id)
    if opp is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    repos.opportunities.patch(opp_id, {"needs_review": False, "review_reason": ""})
    return redirect(request, "opportunity_detail", opp_id=opp_id)


@router.post("/opportunities/{opp_id}/archive", name="opportunity_archive")
def opportunity_archive(
    opp_id: str,
    request: Request,
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
) -> RedirectResponse:
    """Admin archives (soft-deletes) an opportunity — marks as duplicate so it disappears from catalog."""
    guard = staff_or_ngo_redirect(request, current_user)
    if guard is not None:
        return guard
    opp = repos.opportunities.get(opp_id)
    if opp is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    repos.opportunities.patch(opp_id, {"is_duplicate": True, "needs_review": False})
    return redirect(request, "opportunities_page")


@router.post("/opportunities/{opp_id}/assign", name="opportunity_assign")
def opportunity_assign(
    opp_id: str,
    request: Request,
    student_id: str = Form(...),
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
) -> RedirectResponse:
    """NGO Admin / Admin: manually assign an opportunity to a specific student as a draft."""
    guard = staff_or_ngo_redirect(request, current_user)
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
    return redirect(request, "opportunity_detail", opp_id=opp_id)


@router.post("/opportunities/{opp_id}/edit", name="opportunity_edit")
def opportunity_edit(
    opp_id: str,
    request: Request,
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
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
    guard = staff_or_ngo_redirect(request, current_user)
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
    return redirect(request, "opportunity_detail", opp_id=opp_id)


@router.get("/opportunities/{opp_id}", response_class=HTMLResponse, name="opportunity_detail")
def opportunity_detail(
    opp_id: str,
    request: Request,
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
) -> HTMLResponse:
    guard = staff_or_ngo_redirect(request, current_user)
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
    current_user: AppUser | None = Depends(current_user),
) -> HTMLResponse:
    guard = staff_redirect(request, current_user)
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
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
) -> HTMLResponse:
    user = staff_required(current_user)
    return render_stats(request, repos, user)


@router.get("/ui/drafts", response_class=HTMLResponse)
def ui_drafts(
    request: Request,
    status_filter: _STATUS_LITERAL = "pending_approval",
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
) -> HTMLResponse:
    user = staff_or_ngo_required(current_user)
    return render_drafts_panel(request, repos, status_filter, user)


@router.get("/ui/users", response_class=HTMLResponse)
def ui_users(
    request: Request,
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
) -> HTMLResponse:
    user = staff_required(current_user)
    return render_user_panel(request, repos, user)


@router.post("/ui/users/{user_id}/toggle-active", response_class=HTMLResponse)
def ui_toggle_user_active(
    user_id: str,
    request: Request,
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
) -> HTMLResponse:
    user = staff_required(current_user)
    target = repos.users.get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    repos.users.patch(user_id, {"is_active": not target.is_active})
    return render_user_panel(
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
    repos: Repos = Depends(repos_dep),
    distributor: DistributorAgent = Depends(distributor_dep),
    current_user: AppUser | None = Depends(current_user),
) -> HTMLResponse:
    user = staff_required(current_user)
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
    return render_drafts_with_stats(request, repos, status_filter, user, flash=flash)


@router.post("/ui/drafts/{draft_id}/reject", response_class=HTMLResponse)
def ui_reject(
    draft_id: str,
    request: Request,
    status_filter: _STATUS_LITERAL = "pending_approval",
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
) -> HTMLResponse:
    user = staff_required(current_user)
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
    return render_drafts_with_stats(
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
    repos: Repos = Depends(repos_dep),
    distributor: DistributorAgent = Depends(distributor_dep),
    current_user: AppUser | None = Depends(current_user),
) -> HTMLResponse:
    user = staff_required(current_user)
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
                distributor.send_one(updated)
            except Exception as exc:
                logger.bind(draft_id=draft_id, error=str(exc)).warning("ui_save_edit.send_failed")
    return render_drafts_panel(request, repos, status_filter, user)


@router.post("/ui/poll", response_class=HTMLResponse)
def ui_poll(
    request: Request,
    repos: Repos = Depends(repos_dep),
    ingestion: IngestionAgent = Depends(ingestion_dep),
    current_user: AppUser | None = Depends(current_user),
) -> HTMLResponse:
    user = staff_required(current_user)
    processed, pending = ingestion.poll_unread()
    flash = f"Polled inbox: {len(processed)} message(s), {len(pending)} pending approval."
    return render_stats(request, repos, user, flash=flash)


@router.post("/ui/remind", response_class=HTMLResponse)
def ui_remind(
    request: Request,
    repos: Repos = Depends(repos_dep),
    reminder: ReminderAgent = Depends(reminder_dep),
    current_user: AppUser | None = Depends(current_user),
) -> HTMLResponse:
    user = staff_required(current_user)
    sent = reminder.run()
    return render_stats(request, repos, user, flash=f"Reminder sweep sent {sent} reminder(s).")


@router.post("/ui/digest", response_class=HTMLResponse)
def ui_digest(
    request: Request,
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
) -> HTMLResponse:
    user = staff_required(current_user)
    drafts = DigestAgent(repos=repos).build_and_queue()
    return render_stats(
        request,
        repos,
        user,
        flash=f"Weekly digest queued {len(drafts)} draft(s) for review.",
    )


@router.post("/ui/simulate", response_class=HTMLResponse)
def ui_simulate(
    request: Request,
    repos: Repos = Depends(repos_dep),
    ingestion: IngestionAgent = Depends(ingestion_dep),
    current_user: AppUser | None = Depends(current_user),
) -> HTMLResponse:
    user = staff_required(current_user)
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
    return render_stats(request, repos, user, flash=flash)


@router.post("/admin/students/add", name="student_manual_add")
def student_manual_add(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    school_name: str = Form(""),
    level: str = Form("other"),
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
) -> RedirectResponse:
    guard = staff_or_ngo_redirect(request, current_user)
    if guard is not None:
        return guard
    import secrets as _sec
    from evk.auth import hash_access_key

    email = email.strip().lower()
    if repos.students.get_by_email(email):
        return redirect(request, "students_page")
    try:
        lv = StudentLevel(level.strip()) if level.strip() else StudentLevel.OTHER
    except ValueError:
        lv = StudentLevel.OTHER
    sid = f"student_{_sec.token_hex(6)}"
    student = Student(
        id=sid,
        name=name.strip() or email.split("@")[0],
        email=email,
        level=lv,
        school_name=school_name.strip(),
        opted_in=False,
    )
    repos.students.upsert(student)
    salt = _sec.token_hex(8)
    tmp_pw = _sec.token_urlsafe(16)
    user = AppUser(
        id=f"user_student_{sid}",
        email=email,
        name=name.strip() or email.split("@")[0],
        role=UserRole.STUDENT,
        organization=school_name.strip(),
        student_id=sid,
        access_key_salt=salt,
        access_key_hash=hash_access_key(tmp_pw, salt=salt),
        is_active=False,
    )
    repos.users.upsert(user)
    return redirect(request, "students_page")


@router.post("/admin/students/import", name="students_import")
async def students_import(
    request: Request,
    file: UploadFile = File(...),
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
) -> RedirectResponse:
    """Parse a CSV and bulk-create inactive Student + AppUser records."""
    guard = staff_redirect(request, current_user)
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
    return redirect(request, "students_page")


@router.post("/admin/students/{student_id}/test-notification", name="student_test_notification")
def student_test_notification(
    student_id: str,
    request: Request,
    repos: Repos = Depends(repos_dep),
    inkbox: InkboxClient = Depends(get_inkbox),
    current_user: AppUser | None = Depends(current_user),
) -> RedirectResponse:
    guard = staff_or_ngo_redirect(request, current_user)
    if guard is not None:
        return guard
    student = repos.students.get(student_id)
    if student is None:
        raise HTTPException(status_code=404)
    method = student.preferred_notification_method
    first_name = student.name.split()[0] if student.name else "there"
    try:
        if method in ("whatsapp", "sms") and student.phone:
            from evk.twilio_client import TwilioClient
            twilio = TwilioClient()
            if method == "whatsapp":
                twilio.send_whatsapp(to=student.phone, body=f"[EVkids Test] Hi {first_name}, this is a test to confirm WhatsApp delivery is working.")
            else:
                twilio.send_sms(to=student.phone, body=f"[EVkids Test] Hi {first_name}, this is a test to confirm SMS delivery is working.")
        else:
            inkbox.send(
                to=[student.email],
                subject="[EVkids Test] Notification test",
                body_text=f"Hi {first_name},\n\nThis is a test notification from EVkids to confirm your email delivery is working.\n\nMethod: {method}\nFrequency: {student.notification_frequency}\n\n— EVkids Team",
                body_html="",
            )
        logger.bind(student_id=student_id, method=method).info("test_notification.sent")
    except Exception as exc:
        logger.bind(student_id=student_id, error=str(exc)).warning("test_notification.failed")
    return redirect(request, "students_page")


@router.post("/admin/students/{student_id}/activate", name="student_activate")
def student_activate(
    student_id: str,
    request: Request,
    repos: Repos = Depends(repos_dep),
    auth: AuthService = Depends(auth_dep),
    current_user: AppUser | None = Depends(current_user),
) -> RedirectResponse:
    guard = staff_redirect(request, current_user)
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
    return redirect(request, "students_page")


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
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
) -> RedirectResponse:
    guard = staff_or_ngo_redirect(request, current_user)
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
    return redirect(request, "students_page")


@router.post("/ui/scrape", response_class=HTMLResponse)
def ui_scrape(
    request: Request,
    source: str = Form(...),
    repos: Repos = Depends(repos_dep),
    inkbox: InkboxClient = Depends(get_inkbox),
    current_user: AppUser | None = Depends(current_user),
) -> HTMLResponse:
    user = staff_required(current_user)
    from evk.agents.scraper import WebScraperAgent, SCRAPER_SOURCES
    if source not in SCRAPER_SOURCES:
        raise HTTPException(status_code=400, detail=f"Unknown source: {source!r}")
    try:
        count = WebScraperAgent(repos=repos, inkbox=inkbox).scrape(source)
        flash = f"Scraped {SCRAPER_SOURCES[source]['name']}: {count} opportunit{'y' if count == 1 else 'ies'} queued for review."
    except Exception as exc:
        flash = f"Scrape failed for {source}: {exc}"
    return render_stats(request, repos, user, flash=flash)
