"""Student dashboard, profile, suggestions, outcomes."""

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

router = APIRouter(tags=["ui-student"])

@router.get("/app/student", response_class=HTMLResponse, name="student_dashboard")
def student_dashboard(
    request: Request,
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
    flash: str | None = None,
) -> HTMLResponse:
    if current_user is None:
        return redirect(request, "landing")
    if current_user.role is not UserRole.STUDENT:
        return redirect(request, role_home(current_user))
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
    outcome_map: dict[str, dict] = {}
    if student:
        outcome_map = {o.get("opp_id"): o for o in student.outcomes if o.get("opp_id")}
    recommended = recommend_for_student(student, repos, decorate_opps, limit=12)
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
            "outcome_map": outcome_map,
            "recommended_opportunities": recommended,
            "detail_route": "student_opportunity_detail",
            "flash": flash,
        },
    )


@router.get("/opportunities/suggest", response_class=HTMLResponse, name="opportunity_suggest_page")
def opportunity_suggest_page(
    request: Request,
    current_user: AppUser | None = Depends(current_user),
) -> HTMLResponse:
    """Student: suggest an opportunity they found for admin review."""
    if not current_user:
        return redirect(request, "login_page")  # type: ignore[return-value]
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
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
    title: str = Form(""),
    organization: str = Form(""),
    kind: str = Form("other"),
    url: str = Form(""),
    summary: str = Form(""),
    deadline_str: str = Form(""),
) -> RedirectResponse:
    """Create a student-suggested opportunity; lands in the review queue."""
    if not current_user:
        return redirect(request, "login_page")  # type: ignore[return-value]
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
    return flash_redirect(
        request,
        "student_dashboard",
        "Thanks! Your suggestion is with our team for review.",
    )


@router.get("/app/student/opportunities/{opp_id}", response_class=HTMLResponse, name="student_opportunity_detail")
def student_opportunity_detail(
    opp_id: str,
    request: Request,
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
    flash: str | None = None,
) -> HTMLResponse:
    if current_user is None:
        return redirect(request, "login_page")  # type: ignore[return-value]
    if current_user.role is not UserRole.STUDENT:
        return redirect(request, role_home(current_user))  # type: ignore[return-value]
    student = repos.students.get(current_user.student_id) if current_user.student_id else None
    opp = repos.opportunities.get(opp_id)
    if opp is None:
        return flash_redirect(request, "student_dashboard", "That opportunity is not available.")  # type: ignore[return-value]
    from evk.agents.personalizer import score_match

    match = score_match(student, opp) if student else None
    today = datetime.now(UTC).date()
    days_until: int | None = None
    if opp.deadline:
        delta = (opp.deadline.date() - today).days
        days_until = delta if delta >= 0 else None
    outcome = next((o for o in (student.outcomes if student else []) if o.get("opp_id") == opp_id), None)
    return templates.TemplateResponse(
        request,
        "student_opportunity_detail.html",
        {
            "request": request,
            "current_user": current_user,
            "wiring": describe_wiring(),
            "student": student,
            "opp": opp,
            "days_until": days_until,
            "match": match,
            "outcome": outcome,
            "flash": flash,
        },
    )


@router.post("/student/outcome", name="student_outcome_save")
def student_outcome_save(
    request: Request,
    opp_id: str = Form(...),
    status: str = Form(""),
    notes: str = Form(""),
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
) -> RedirectResponse:
    if current_user is None or current_user.student_id is None:
        return redirect(request, "login_page")
    student = repos.students.get(current_user.student_id)
    if student is None:
        return redirect(request, "student_dashboard")
    outcomes = [o for o in student.outcomes if o.get("opp_id") != opp_id]
    if status:
        outcomes.append({
            "opp_id": opp_id,
            "status": status,
            "notes": notes.strip(),
            "updated_at": datetime.now(UTC).isoformat(),
        })
    repos.students.patch(current_user.student_id, {"outcomes": outcomes})
    return flash_redirect(
        request,
        "student_opportunity_detail",
        "Thanks — your update was saved.",
        opp_id=opp_id,
    )


@router.get("/profile", response_class=HTMLResponse, name="student_profile_page")
def student_profile_page(
    request: Request,
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
    flash: str | None = None,
) -> HTMLResponse:
    if current_user is None:
        return redirect(request, "login_page")
    student = repos.students.get(current_user.student_id) if current_user.student_id else None
    return templates.TemplateResponse(
        request,
        "student_profile.html",
        {"request": request, "current_user": current_user, "student": student,
         "wiring": describe_wiring(), "flash": flash},
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
    opted_in: str = Form(default="on"),
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
) -> RedirectResponse:
    if current_user is None or current_user.student_id is None:
        return redirect(request, "login_page")
    repos.students.patch(current_user.student_id, {
        "career_interests": career_interests,
        "opportunity_types_sought": opportunity_types,
        "notification_frequency": notification_frequency,
        "preferred_notification_method": preferred_notification_method,
        "phone": phone.strip(),
        "bio": bio.strip(),
        "opted_in": opted_in == "on",
    })
    return flash_redirect(request, "student_profile_page", "Profile saved — we'll use this for your next matches.")

