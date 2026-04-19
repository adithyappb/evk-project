"""Domain models shared across agents, Firestore, and the Gemini response schema.

All models are Pydantic v2 with explicit, stable field names that double as
Firestore field names and Gemini JSON-schema properties.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, EmailStr, Field, HttpUrl


def _utcnow() -> datetime:
    return datetime.now(UTC)


class OpportunityKind(StrEnum):
    """What kind of opportunity the newsletter is offering."""

    SCHOLARSHIP = "scholarship"
    INTERNSHIP = "internship"
    HACKATHON = "hackathon"
    COMPETITION = "competition"
    CONFERENCE = "conference"
    FELLOWSHIP = "fellowship"
    JOB = "job"
    GRANT = "grant"
    PROGRAM = "program"
    OTHER = "other"


class StudentLevel(StrEnum):
    HIGH_SCHOOL = "high_school"
    UNDERGRAD = "undergrad"
    GRAD = "grad"
    OTHER = "other"


# --------------------------------------------------------------------------- #
# Gemini-facing models (classifier output schema)                             #
# --------------------------------------------------------------------------- #


class ExtractedOpportunity(BaseModel):
    """Structured opportunity extracted from a newsletter by the classifier.

    This schema is passed to Gemini as `response_schema`; keep it flat and
    avoid Python-only types so Vertex AI accepts it.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(description="Short human-readable opportunity title.")
    kind: OpportunityKind = Field(description="Category of opportunity.")
    organization: str = Field(description="Sponsoring organisation or company.")
    summary: str = Field(description="1-3 sentence neutral summary of what this offers.")
    eligibility: str = Field(
        default="", description="Who can apply: grade level, fields, citizenship, etc."
    )
    deadline_iso: str | None = Field(
        default=None,
        description="Application deadline as ISO-8601 date (YYYY-MM-DD) or null if unknown.",
    )
    url: str | None = Field(default=None, description="Primary application / info URL.")
    location: str = Field(default="", description="City/country/remote, or empty.")
    tags: list[str] = Field(
        default_factory=list,
        description="Lowercase topic tags like 'ai', 'biology', 'women-in-stem'.",
    )
    fields_of_study: list[str] = Field(
        default_factory=list,
        description="Relevant fields (e.g. 'computer science', 'mechanical engineering').",
    )
    min_level: StudentLevel = Field(
        default=StudentLevel.OTHER,
        description="Minimum student level that can apply.",
    )


class ClassifierResult(BaseModel):
    """Top-level classifier response."""

    model_config = ConfigDict(extra="forbid")

    is_opportunity: bool = Field(
        description=(
            "True only if the email advertises a concrete opportunity a student can apply to "
            "(scholarship, internship, hackathon, etc.). False for general news/marketing."
        )
    )
    confidence: float = Field(ge=0.0, le=1.0, description="Classifier confidence 0-1.")
    reasoning: str = Field(description="Short human-readable justification.")
    opportunities: list[ExtractedOpportunity] = Field(
        default_factory=list,
        description="One entry per distinct opportunity found. Empty if is_opportunity is false.",
    )


# --------------------------------------------------------------------------- #
# Firestore documents                                                         #
# --------------------------------------------------------------------------- #


class FirestoreDoc(BaseModel):
    """Base for all Firestore-persisted documents."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(description="Firestore document ID.")
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Student(FirestoreDoc):
    """A student profile used for opportunity matching."""

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


class Opportunity(FirestoreDoc):
    """Canonical opportunity record. ID is a stable hash of source+title+deadline."""

    title: str
    kind: OpportunityKind
    organization: str
    summary: str
    eligibility: str = ""
    deadline: datetime | None = None
    url: HttpUrl | None = None
    location: str = ""
    tags: list[str] = Field(default_factory=list)
    fields_of_study: list[str] = Field(default_factory=list)
    min_level: StudentLevel = StudentLevel.OTHER
    source_raw_email_id: str | None = None
    source_subject: str = ""
    source_sender: str = ""


class RawEmailStatus(StrEnum):
    RECEIVED = "received"
    CLASSIFIED = "classified"
    SKIPPED = "skipped"
    FAILED = "failed"


class RawEmail(FirestoreDoc):
    """An incoming email pulled from Inkbox. Preserved for audit + idempotency."""

    inkbox_message_id: str = Field(description="Inkbox msg_ id (NOT the RFC 5322 id).")
    rfc_message_id: str | None = None
    thread_id: str | None = None
    from_address: str
    subject: str
    body_text: str = ""
    body_html: str = ""
    received_at: datetime = Field(default_factory=_utcnow)
    status: RawEmailStatus = RawEmailStatus.RECEIVED
    classification_error: str | None = None
    extracted_opportunity_ids: list[str] = Field(default_factory=list)


class DraftStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    SENT = "sent"
    FAILED = "failed"


class DraftMessage(FirestoreDoc):
    """A personalised draft email awaiting human approval before being sent."""

    student_id: str
    opportunity_id: str
    to_email: EmailStr
    subject: str
    body_text: str
    body_html: str = ""
    match_score: float = Field(ge=0.0, le=1.0)
    match_reasons: list[str] = Field(default_factory=list)
    status: DraftStatus = DraftStatus.PENDING_APPROVAL
    approved_by: str | None = None
    approved_at: datetime | None = None
    sent_at: datetime | None = None
    inkbox_message_id: str | None = None
    send_error: str | None = None


class ReminderLog(FirestoreDoc):
    """Records when we sent a reminder so we don't duplicate."""

    student_id: str
    opportunity_id: str
    days_before: int
    sent_at: datetime = Field(default_factory=_utcnow)


__all__ = [
    "ClassifierResult",
    "DraftMessage",
    "DraftStatus",
    "ExtractedOpportunity",
    "Opportunity",
    "OpportunityKind",
    "RawEmail",
    "RawEmailStatus",
    "ReminderLog",
    "Student",
    "StudentLevel",
]
