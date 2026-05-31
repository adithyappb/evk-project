"""Domain models shared across agents, Firestore, and the Gemini response schema.

All models are Pydantic v2 with explicit, stable field names that double as
Firestore field names and Gemini JSON-schema properties.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

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
    CAREER_FAIR = "career fair"
    JOB_TRAINING = "job training"
    SUMMER_JOB = "summer job"
    COLLEGE_SUPPORT = "college support"
    GRANT = "grant"
    PROGRAM = "program"
    OTHER = "other"

    @classmethod
    def _missing_(cls, value: object) -> OpportunityKind | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip().lower()
        aliases = {
            "career-fair": cls.CAREER_FAIR,
            "job-training": cls.JOB_TRAINING,
            "summer-job": cls.SUMMER_JOB,
            "college-support": cls.COLLEGE_SUPPORT,
        }
        return aliases.get(normalized)


class StudentLevel(StrEnum):
    GRADE_11 = "11th grade"
    GRADE_12 = "12th grade"
    COLLEGE = "College year 1-4"
    UNDERGRAD = "College year 1-4"
    GRAD = "Graduate"
    ALUMNI = "Alumni"
    OTHER = "other"

    @classmethod
    def _missing_(cls, value: object) -> StudentLevel | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip().lower()
        aliases = {
            "11": cls.GRADE_11,
            "grade 11": cls.GRADE_11,
            "high_school": cls.GRADE_12,
            "high school": cls.GRADE_12,
            "12": cls.GRADE_12,
            "grade 12": cls.GRADE_12,
            "college": cls.COLLEGE,
            "undergrad": cls.COLLEGE,
            "undergraduate": cls.COLLEGE,
            "college year 1-4": cls.COLLEGE,
            "grad": cls.GRAD,
            "graduate": cls.GRAD,
            "graduate student": cls.GRAD,
            "alumni": cls.ALUMNI,
            "other": cls.OTHER,
        }
        return aliases.get(normalized)


# --------------------------------------------------------------------------- #
# Gemini-facing models (classifier output schema)                             #
# --------------------------------------------------------------------------- #


class ExtractedOpportunity(BaseModel):
    """Structured opportunity extracted from a newsletter by the classifier.

    This schema is passed to Gemini as `response_schema`; keep it flat and
    avoid Python-only types so Vertex AI accepts it.
    """

    model_config = ConfigDict(
        extra="ignore",          # tolerate bonus fields from Gemini
        populate_by_name=True,   # allow both field name and alias
    )

    # Core fields — all have defaults so partial Gemini responses don't hard-fail.
    # Aliases catch Gemini's preferred alternative names (e.g. "name" vs "title").
    title: str = Field(default="", alias="name", description="Short human-readable opportunity title.")
    kind: OpportunityKind = Field(default=OpportunityKind.OTHER, description="Category of opportunity.")
    organization: str = Field(default="", alias="org", description="Sponsoring organisation or company.")
    summary: str = Field(default="", alias="description", description="1-3 sentence neutral summary of what this offers.")
    eligibility: str = Field(
        default="", description="Who can apply: grade level, fields, citizenship, etc."
    )
    deadline_iso: str | None = Field(
        default=None,
        alias="deadline",
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
    needs_review: bool = Field(
        default=False,
        description=(
            "True when the classifier is uncertain — deadline missing/inferred, "
            "eligibility vague, event may have passed, or URL absent. "
            "Admin must review before this reaches students."
        ),
    )
    review_reason: str = Field(
        default="",
        description="Plain-English explanation of why needs_review is true.",
    )

    @classmethod
    def model_validate(cls, obj: object, **kwargs: object) -> "ExtractedOpportunity":  # type: ignore[override]
        """Coerce None strings to empty string before validation."""
        if isinstance(obj, dict):
            obj = {
                k: ("" if v is None and k in {"eligibility", "location", "organization", "summary", "title"} else v)
                for k, v in obj.items()
            }
        return super().model_validate(obj, **kwargs)


class ClassifierResult(BaseModel):
    """Top-level classifier response."""

    model_config = ConfigDict(extra="ignore")

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

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str
    email: EmailStr
    level: StudentLevel
    school_name: str = Field(default="", alias="school")
    graduation_year: int | None = None
    fields_of_study: list[str] = Field(default_factory=list)
    career_interests: list[str] = Field(default_factory=list, alias="interests")
    opportunity_types_sought: list[str] = Field(default_factory=list)
    boston_resident: bool = False
    first_generation: bool = False
    location: str = ""
    bio: str = ""
    opted_in: bool = True
    phone: str = ""
    preferred_notification_method: str = "email"
    notification_frequency: str = "weekly"

    @property
    def school(self) -> str:
        return self.school_name

    @property
    def interests(self) -> list[str]:
        return self.career_interests


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
    is_duplicate: bool = False
    # Admin-review flag — set by classifier when deadline/eligibility is uncertain.
    # Opportunities with needs_review=True are held in the review queue and never
    # sent to students until an admin clears them.
    needs_review: bool = False
    review_reason: str = ""
    embedding: list[float] = Field(default_factory=list)


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


class UserRole(StrEnum):
    STUDENT = "student"
    NGO_ADMIN = "ngo_admin"
    ADMIN = "admin"


class AppUser(FirestoreDoc):
    """A person who can access the EVKids dashboards."""

    email: EmailStr
    name: str
    role: UserRole
    organization: str = ""
    student_id: str | None = None
    is_active: bool = True
    access_key_salt: str
    access_key_hash: str
    last_login_at: datetime | None = None
    activation_token: str | None = None
    activation_token_expires: datetime | None = None


class LoginChallenge(FirestoreDoc):
    """A single MFA challenge issued after the primary factor is validated."""

    user_id: str
    email: EmailStr
    code_hash: str
    expires_at: datetime
    used_at: datetime | None = None
    purpose: Literal["login", "reset"] = "login"


class Session(FirestoreDoc):
    """Server-side session stored for role-aware dashboard access."""

    user_id: str
    role: UserRole
    expires_at: datetime
    last_seen_at: datetime = Field(default_factory=_utcnow)


__all__ = [
    "AppUser",
    "ClassifierResult",
    "DraftMessage",
    "DraftStatus",
    "ExtractedOpportunity",
    "LoginChallenge",
    "Opportunity",
    "OpportunityKind",
    "RawEmail",
    "RawEmailStatus",
    "ReminderLog",
    "Session",
    "Student",
    "StudentLevel",
    "UserRole",
]
