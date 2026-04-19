"""Dashboard HTML routes.

Server-rendered Jinja + HTMX fragments. The factory module supplies all
runtime dependencies so dependency overrides in tests are trivial.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from evk.agents.digest import DigestAgent
from evk.agents.distributor import DistributorAgent
from evk.agents.ingestion import IngestionAgent
from evk.agents.reminder import ReminderAgent
from evk.factory import describe_wiring, get_inkbox, get_repos
from evk.firestore_repo import Repos
from evk.inkbox_client import InboundMessage, InkboxClient
from evk.models import DraftMessage, DraftStatus, Opportunity
from evk.ui import TEMPLATES_DIR

router = APIRouter(tags=["ui"])
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


STATUS_TABS: list[tuple[str, str]] = [
    ("Pending", "pending_approval"),
    ("Approved", "approved"),
    ("Sent", "sent"),
    ("Rejected", "rejected"),
    ("Failed", "failed"),
]

_STATUS_LITERAL = Literal["pending_approval", "approved", "sent", "rejected", "failed"]


# --------------------------------------------------------------------------- #
# Dependencies (reuse factory; overridden in tests)                           #
# --------------------------------------------------------------------------- #


def _repos_dep() -> Repos:
    return get_repos()


def _inkbox_dep() -> InkboxClient:
    return get_inkbox()


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


# --------------------------------------------------------------------------- #
# View model                                                                  #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class OpportunityView:
    """An Opportunity decorated with the UI-only `days_until` field."""

    inner: Opportunity
    days_until: int | None

    # Jinja-friendly proxies ---------------------------------------------------
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
    """Annotate each opportunity with `days_until` (None for rolling/expired).

    Expired deadlines get `None` and sink to the bottom; rolling opportunities
    also sit at the bottom.
    """
    today = datetime.now(UTC).date()
    out: list[OpportunityView] = []
    for o in opps:
        days: int | None = None
        if o.deadline is not None:
            delta = (o.deadline.date() - today).days
            days = delta if delta >= 0 else None  # past → treat as rolling
        out.append(OpportunityView(inner=o, days_until=days))
    out.sort(key=lambda v: (v.days_until is None, v.days_until or 10**9))
    return out


def _stats(repos: Repos) -> dict[str, int]:
    today = date.today()
    drafts = repos.drafts.list_all()
    pending = sum(1 for d in drafts if d.status == DraftStatus.PENDING_APPROVAL)
    sent_today = sum(
        1
        for d in drafts
        if d.status == DraftStatus.SENT and d.sent_at and d.sent_at.date() == today
    )
    return {
        "pending": pending,
        "sent_today": sent_today,
        "opportunities": len(repos.opportunities.list_all()),
        "students": len(repos.students.list_all()),
    }


def _status_counts(repos: Repos) -> dict[str, int]:
    counts = dict.fromkeys((v for _, v in STATUS_TABS), 0)
    for d in repos.drafts.list_all():
        counts[d.status.value] = counts.get(d.status.value, 0) + 1
    return counts


def _render_stats(request: Request, repos: Repos, flash: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_stats.html",
        {"stats": _stats(repos), "flash": flash, "wiring": describe_wiring()},
    )


def _render_drafts(request: Request, repos: Repos, active_status: str) -> HTMLResponse:
    try:
        status = DraftStatus(active_status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid status") from exc
    drafts: list[DraftMessage] = repos.drafts.list_by_status(status, limit=200)
    drafts.sort(key=lambda d: d.created_at, reverse=True)
    return templates.TemplateResponse(
        request,
        "_drafts.html",
        {"drafts": drafts, "active_status": active_status},
    )


# --------------------------------------------------------------------------- #
# Routes                                                                      #
# --------------------------------------------------------------------------- #


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, repos: Repos = Depends(_repos_dep)) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "wiring": describe_wiring(),
            "stats": _stats(repos),
            "status_counts": _status_counts(repos),
            "students": repos.students.list_all(limit=50),
            "opportunities": _decorate_opps(repos.opportunities.list_all(limit=100)),
            "status_tabs": STATUS_TABS,
            "active_status": "pending_approval",
            "flash": None,
        },
    )


@router.get("/ui/stats", response_class=HTMLResponse)
def ui_stats(request: Request, repos: Repos = Depends(_repos_dep)) -> HTMLResponse:
    return _render_stats(request, repos)


@router.get("/ui/drafts", response_class=HTMLResponse)
def ui_drafts(
    request: Request,
    status_filter: _STATUS_LITERAL = "pending_approval",
    repos: Repos = Depends(_repos_dep),
) -> HTMLResponse:
    return _render_drafts(request, repos, status_filter)


@router.post("/ui/drafts/{draft_id}/approve", response_class=HTMLResponse)
def ui_approve(
    draft_id: str,
    request: Request,
    repos: Repos = Depends(_repos_dep),
    distributor: DistributorAgent = Depends(_distributor_dep),
) -> HTMLResponse:
    draft = repos.drafts.get(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="draft not found")
    if draft.status not in {DraftStatus.PENDING_APPROVAL, DraftStatus.APPROVED}:
        raise HTTPException(status_code=409, detail="wrong status")
    repos.drafts.patch(
        draft_id,
        {
            "status": DraftStatus.APPROVED.value,
            "approved_by": "dashboard",
            "approved_at": datetime.now(UTC),
        },
    )
    draft = repos.drafts.get(draft_id)
    assert draft is not None
    try:
        draft = distributor.send_one(draft)
    except Exception:  # pragma: no cover — distributor itself records failure
        draft = repos.drafts.get(draft_id) or draft
    return templates.TemplateResponse(request, "_draft_row.html", {"d": draft})


@router.post("/ui/drafts/{draft_id}/reject", response_class=HTMLResponse)
def ui_reject(
    draft_id: str,
    request: Request,
    repos: Repos = Depends(_repos_dep),
) -> HTMLResponse:
    draft = repos.drafts.get(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="draft not found")
    repos.drafts.patch(
        draft_id,
        {
            "status": DraftStatus.REJECTED.value,
            "approved_by": "dashboard",
            "approved_at": datetime.now(UTC),
        },
    )
    draft = repos.drafts.get(draft_id)
    assert draft is not None
    return templates.TemplateResponse(request, "_draft_row.html", {"d": draft})


@router.post("/ui/poll", response_class=HTMLResponse)
def ui_poll(
    request: Request,
    repos: Repos = Depends(_repos_dep),
    ingestion: IngestionAgent = Depends(_ingestion_dep),
) -> HTMLResponse:
    processed, pending = ingestion.poll_unread()
    flash = f"Polled Inkbox — {len(processed)} message(s) · {len(pending)} pending approval"
    return _render_stats(request, repos, flash=flash)


@router.post("/ui/remind", response_class=HTMLResponse)
def ui_remind(
    request: Request,
    repos: Repos = Depends(_repos_dep),
    reminder: ReminderAgent = Depends(_reminder_dep),
) -> HTMLResponse:
    sent = reminder.run()
    flash = f"Reminder sweep — {sent} reminder(s) sent"
    return _render_stats(request, repos, flash=flash)


@router.post("/ui/digest", response_class=HTMLResponse)
def ui_digest(
    request: Request,
    repos: Repos = Depends(_repos_dep),
) -> HTMLResponse:
    """Build the weekly digest drafts (one per opted-in student) for approval."""
    drafts = DigestAgent(repos=repos).build_and_queue()
    flash = f"Weekly digest queued — {len(drafts)} draft(s) pending approval"
    return _render_stats(request, repos, flash=flash)


@router.post("/ui/simulate", response_class=HTMLResponse)
def ui_simulate(
    request: Request,
    repos: Repos = Depends(_repos_dep),
    ingestion: IngestionAgent = Depends(_ingestion_dep),
) -> HTMLResponse:
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
        f"Newsletter ingested · status {raw.status.value} · "
        f"{len(raw.extracted_opportunity_ids)} opportunit"
        f"{'y' if len(raw.extracted_opportunity_ids) == 1 else 'ies'}"
    )
    return _render_stats(request, repos, flash=flash)


__all__ = ["router"]
