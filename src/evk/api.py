"""FastAPI application.

Routes:

* ``POST /webhooks/inkbox``          — HMAC-verified inbound webhook.
* ``GET  /healthz``                  — cheap liveness (never hits externals).
* ``GET  /health``                   — deep health probe (repos + Inkbox + Gemini).
* ``/admin/*``                       — drafts / opportunities / students / poll.
  Guarded by an **optional** bearer token (``ADMIN_API_TOKEN``); also front
  behind Cloud Run IAM / IAP in production for defence in depth.
* ``/``, ``/ui/*``                   — Jinja + HTMX dashboard (see ``evk.ui.routes``).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field

from evk.agents.distributor import DistributorAgent
from evk.agents.ingestion import IngestionAgent
from evk.config import Settings, get_settings
from evk.factory import describe_wiring, get_gemini, get_inkbox, get_repos
from evk.firestore_repo import Repos
from evk.inkbox_client import (
    InkboxClient,
    WebhookVerificationError,
    verify_webhook_signature,
)
from evk.logging import configure_logging, logger
from evk.models import (
    DraftMessage,
    DraftStatus,
    Opportunity,
    Student,
    StudentLevel,
)
from evk.ui.routes import router as _ui_router

# --------------------------------------------------------------------------- #
# Lifespan + app                                                              #
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    configure_logging()
    logger.info("evk.api.starting")
    # Warm heavy singletons so the first request isn't penalised.
    settings = get_settings()
    try:
        get_inkbox()
        get_repos()
    except Exception:
        logger.exception("evk.api.client_init_failed")

    if settings.auto_poll:
        import threading
        import time

        def _poll_loop() -> None:
            while True:
                try:
                    repos = get_repos()
                    inkbox = get_inkbox()
                    agent = IngestionAgent(repos=repos, inkbox=inkbox)
                    agent.poll_unread()
                    logger.info("scheduler.poll_ok")
                except Exception:
                    logger.exception("scheduler.poll_failed")
                time.sleep(settings.poll_interval_minutes * 60)

        def _reminder_loop() -> None:
            # Run reminders once per day (checked every hour; agent is idempotent).
            while True:
                time.sleep(3600)
                try:
                    from evk.agents.reminder import ReminderAgent
                    repos = get_repos()
                    inkbox = get_inkbox()
                    sent = ReminderAgent(repos=repos, inkbox=inkbox).run()
                    if sent:
                        logger.bind(sent=sent).info("scheduler.reminders_sent")
                except Exception:
                    logger.exception("scheduler.reminder_failed")

        threading.Thread(target=_poll_loop, daemon=True, name="evk-poll").start()
        threading.Thread(target=_reminder_loop, daemon=True, name="evk-remind").start()
        logger.bind(interval_minutes=settings.poll_interval_minutes).info("scheduler.started")

    yield
    logger.info("evk.api.stopping")


app = FastAPI(
    title="EVK: Opportunity Pipeline",
    version="0.1.0",
    description=(
        "Ingest newsletters via Inkbox, classify with Gemini, personalise per student, "
        "and distribute approved emails back via Inkbox."
    ),
    lifespan=_lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(_ui_router)


# --------------------------------------------------------------------------- #
# Dependencies                                                                #
# --------------------------------------------------------------------------- #


def _repos() -> Repos:
    return get_repos()


def _inkbox() -> InkboxClient:
    return get_inkbox()


def _ingestion(
    repos: Repos = Depends(_repos), inkbox: InkboxClient = Depends(_inkbox)
) -> IngestionAgent:
    return IngestionAgent(repos=repos, inkbox=inkbox)


def _distributor(
    repos: Repos = Depends(_repos), inkbox: InkboxClient = Depends(_inkbox)
) -> DistributorAgent:
    return DistributorAgent(repos=repos, inkbox=inkbox)


def _require_admin(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """Bearer-token guard, only active when ``ADMIN_API_TOKEN`` is configured."""
    expected = settings.admin_api_token
    if not expected:
        return
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if authorization.split(None, 1)[1].strip() != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# --------------------------------------------------------------------------- #
# Health                                                                      #
# --------------------------------------------------------------------------- #


@app.get("/healthz")
def healthz() -> dict[str, object]:
    """Cheap liveness probe — never hits external services."""
    return {"status": "ok", "time": datetime.now(UTC).isoformat()}


def _probe(label: str, check) -> tuple[str, dict[str, object], bool]:
    try:
        result = check()
        ok = result is not False
        return label, {"ok": ok}, ok
    except Exception as exc:
        return label, {"ok": False, "error": str(exc)[:200]}, False


@app.get("/health")
def health() -> dict[str, object]:
    """Deep readiness probe — 200 if all deps OK, 503 otherwise."""
    probes = [
        _probe("repos", lambda: get_repos().opportunities.list_all(limit=1) or None),
        _probe("inkbox", lambda: get_inkbox() and None),
        _probe("gemini", lambda: getattr(get_gemini(), "healthcheck", lambda: True)()),
    ]
    checks = {label: payload for label, payload, _ in probes}
    ok = all(passed for _, _, passed in probes)
    body = {
        "status": "ok" if ok else "degraded",
        "time": datetime.now(UTC).isoformat(),
        "wiring": describe_wiring(),
        "checks": checks,
    }
    if not ok:
        raise HTTPException(status_code=503, detail=body)
    return body


# --------------------------------------------------------------------------- #
# Webhook                                                                     #
# --------------------------------------------------------------------------- #


class InkboxWebhookPayload(BaseModel):
    """Inkbox webhook envelope — permissive; we only rely on event + message id."""

    event: str
    data: dict | None = None


@app.post("/webhooks/inkbox", status_code=status.HTTP_202_ACCEPTED)
async def inkbox_webhook(
    request: Request,
    ingestion: IngestionAgent = Depends(_ingestion),
    inkbox: InkboxClient = Depends(_inkbox),
    x_inkbox_request_id: str | None = Header(default=None),
    x_inkbox_timestamp: str | None = Header(default=None),
    x_inkbox_signature: str | None = Header(default=None),
) -> dict:
    raw_body = await request.body()
    try:
        verify_webhook_signature(
            raw_body=raw_body,
            request_id=x_inkbox_request_id or "",
            timestamp=x_inkbox_timestamp or "",
            signature=x_inkbox_signature or "",
        )
    except WebhookVerificationError as exc:
        logger.bind(reason=str(exc)).warning("webhook.rejected")
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    try:
        payload = InkboxWebhookPayload.model_validate(json.loads(raw_body))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid webhook body") from exc

    if payload.event != "message.received":
        logger.bind(event=payload.event).debug("webhook.ignored_event")
        return {"ignored": True, "event": payload.event}

    data = payload.data or {}
    message_id = str(data.get("id") or data.get("message_id") or "")
    if not message_id:
        raise HTTPException(status_code=400, detail="missing message id in payload")

    msg = inkbox.fetch(message_id)
    if msg is None:
        logger.bind(message_id=message_id).error("webhook.message_not_found")
        raise HTTPException(status_code=404, detail="message not found in Inkbox")

    raw = ingestion.handle_inbound(msg)
    return {"ok": True, "raw_email_id": raw.id, "status": raw.status.value}


# --------------------------------------------------------------------------- #
# Admin router — one place to apply auth, CORS, etc.                          #
# --------------------------------------------------------------------------- #


admin = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(_require_admin)],
)


class DraftView(BaseModel):
    id: str
    student_id: str
    opportunity_id: str
    to_email: EmailStr
    subject: str
    body_text: str
    body_html: str
    match_score: float
    match_reasons: list[str]
    status: DraftStatus
    created_at: datetime
    sent_at: datetime | None = None

    @classmethod
    def from_model(cls, d: DraftMessage) -> DraftView:
        return cls(**d.model_dump(include=set(cls.model_fields.keys())))


class ApprovalBody(BaseModel):
    approver: str = Field(default="admin", description="Name/email of approving human.")
    send_now: bool = Field(
        default=True,
        description="If true, send immediately; else mark approved for a later batch.",
    )


class StudentUpsert(BaseModel):
    id: str
    name: str
    email: EmailStr
    level: StudentLevel
    school: str = ""
    graduation_year: int | None = None
    fields_of_study: list[str] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    location: str = ""
    bio: str = ""
    opted_in: bool = True


class PollResponse(BaseModel):
    processed: int
    pending_drafts: int


@admin.get("/drafts", response_model=list[DraftView])
def list_drafts(
    status_filter: Literal[
        "pending_approval", "approved", "rejected", "sent", "failed"
    ] = "pending_approval",
    limit: int = 100,
    repos: Repos = Depends(_repos),
) -> list[DraftView]:
    drafts = repos.drafts.list_by_status(DraftStatus(status_filter), limit=limit)
    return [DraftView.from_model(d) for d in drafts]


@admin.post("/drafts/{draft_id}/approve", response_model=DraftView)
def approve_draft(
    draft_id: str,
    body: ApprovalBody,
    repos: Repos = Depends(_repos),
    distributor: DistributorAgent = Depends(_distributor),
) -> DraftView:
    draft = repos.drafts.get(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="draft not found")
    if draft.status not in {DraftStatus.PENDING_APPROVAL, DraftStatus.APPROVED}:
        raise HTTPException(
            status_code=409, detail=f"cannot approve draft in status {draft.status.value}"
        )
    repos.drafts.patch(
        draft_id,
        {
            "status": DraftStatus.APPROVED.value,
            "approved_by": body.approver,
            "approved_at": datetime.now(UTC),
        },
    )
    draft = repos.drafts.get(draft_id)
    assert draft is not None
    if body.send_now:
        draft = distributor.send_one(draft)
    return DraftView.from_model(draft)


@admin.post("/drafts/{draft_id}/reject", response_model=DraftView)
def reject_draft(
    draft_id: str,
    body: ApprovalBody,
    repos: Repos = Depends(_repos),
) -> DraftView:
    draft = repos.drafts.get(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="draft not found")
    if draft.status in {DraftStatus.SENT, DraftStatus.REJECTED}:
        raise HTTPException(
            status_code=409, detail=f"cannot reject draft in status {draft.status.value}"
        )
    repos.drafts.patch(
        draft_id,
        {
            "status": DraftStatus.REJECTED.value,
            "approved_by": body.approver,
            "approved_at": datetime.now(UTC),
        },
    )
    draft = repos.drafts.get(draft_id)
    assert draft is not None
    return DraftView.from_model(draft)


@admin.get("/opportunities", response_model=list[Opportunity])
def list_opportunities(
    limit: int = 200,
    repos: Repos = Depends(_repos),
) -> list[Opportunity]:
    return repos.opportunities.list_all(limit=limit)


@admin.get("/students", response_model=list[Student])
def list_students(
    limit: int = 200,
    repos: Repos = Depends(_repos),
) -> list[Student]:
    return repos.students.list_all(limit=limit)


@admin.post("/students", response_model=Student)
def upsert_student(body: StudentUpsert, repos: Repos = Depends(_repos)) -> Student:
    return repos.students.upsert(Student(**body.model_dump()))


@admin.post("/poll", response_model=PollResponse)
def poll_inbox(ingestion: IngestionAgent = Depends(_ingestion)) -> PollResponse:
    processed, pending = ingestion.poll_unread()
    return PollResponse(processed=len(processed), pending_drafts=len(pending))


app.include_router(admin)
