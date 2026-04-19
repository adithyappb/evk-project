"""Local-mode stubs for Gemini and Inkbox — zero external calls.

Stubs give you a fully-working pipeline with no credentials:

* `StubGemini` is a deterministic regex+keyword classifier that produces
  `ClassifierResult` and personalised-copy responses faithful to the real
  schemas. Useful for demos, tests, and UI development.

* `StubInkbox` logs every outbound "send" to a JSONL file so you can see
  what *would* be delivered; inbound messages come from an in-memory queue
  you can seed via CLI (`evk simulate`).
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from evk.config import get_settings
from evk.inkbox_client import InboundMessage, InkboxClient
from evk.logging import logger
from evk.models import (
    ClassifierResult,
    ExtractedOpportunity,
    OpportunityKind,
    StudentLevel,
)

T = TypeVar("T", bound=BaseModel)


# --------------------------------------------------------------------------- #
# Stub Gemini                                                                 #
# --------------------------------------------------------------------------- #


_OPPORTUNITY_KEYWORDS = (
    "apply",
    "deadline",
    "scholarship",
    "internship",
    "hackathon",
    "fellowship",
    "grant",
    "program",
    "competition",
    "conference",
)

_KIND_KEYWORDS: dict[OpportunityKind, tuple[str, ...]] = {
    OpportunityKind.SCHOLARSHIP: ("scholarship", "scholar"),
    OpportunityKind.INTERNSHIP: ("internship", "intern"),
    OpportunityKind.HACKATHON: ("hackathon", "hack"),
    OpportunityKind.COMPETITION: ("competition", "contest", "olympiad", "science fair"),
    OpportunityKind.FELLOWSHIP: ("fellowship", "fellow"),
    OpportunityKind.CONFERENCE: ("conference", "summit"),
    OpportunityKind.GRANT: ("grant", "funding"),
    OpportunityKind.PROGRAM: ("program", "camp", "bootcamp"),
    OpportunityKind.JOB: ("full-time role", "job opening"),
}

_LEVEL_KEYWORDS: dict[StudentLevel, tuple[str, ...]] = {
    StudentLevel.HIGH_SCHOOL: (
        "high school",
        "high-school",
        "grade 9",
        "grade 10",
        "grade 11",
        "grade 12",
    ),
    StudentLevel.UNDERGRAD: (
        "undergrad",
        "undergraduate",
        "bachelor",
        "university student",
        "college",
    ),
    StudentLevel.GRAD: ("graduate", "postgraduate", "phd", "master", "postdoc"),
}

_FIELD_KEYWORDS: tuple[str, ...] = (
    "computer science",
    "software engineering",
    "artificial intelligence",
    "machine learning",
    "mechanical engineering",
    "aerospace engineering",
    "electrical engineering",
    "biology",
    "chemistry",
    "physics",
    "mathematics",
    "robotics",
    "design",
    "business",
    "public policy",
    "environmental science",
)

_URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]]+[^\s<>\"'().,;:!?\[\]]", re.IGNORECASE)
_DATE_RE = re.compile(
    r"\b(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t|tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<year>\d{4})",
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

# Numbered-list splitter: matches "1) " / "1. " / "#1 " at line start.
_SECTION_RE = re.compile(r"^\s*(?:\d+[\.\)]\s+|#\d+\s+)", re.MULTILINE)


class StubGemini:
    """Deterministic stub that mimics GeminiClient's public API."""

    def generate_structured(
        self,
        *,
        prompt: str,
        schema: type[T],
        system_instruction: str | None = None,
        temperature: float = 0.1,
        max_output_tokens: int = 4096,
    ) -> T:
        schema_name = schema.__name__
        if schema_name == "ClassifierResult":
            return self._classify(prompt)  # type: ignore[return-value]
        if schema_name == "_PersonalisedCopy":
            return self._write_copy(prompt, schema)  # type: ignore[return-value]
        return schema.model_construct()  # type: ignore[return-value]

    def generate_text(
        self,
        *,
        prompt: str,
        system_instruction: str | None = None,
        **_: Any,
    ) -> str:
        return prompt[-200:]

    @staticmethod
    def healthcheck() -> bool:
        """Stub is always healthy — no network, no state."""
        return True

    # --- internal ----------------------------------------------------------

    @staticmethod
    def _classify(prompt: str) -> ClassifierResult:
        lower = prompt.lower()
        hit_count = sum(1 for kw in _OPPORTUNITY_KEYWORDS if kw in lower)
        if hit_count < 2:
            return ClassifierResult(
                is_opportunity=False,
                confidence=0.85,
                reasoning="stub: fewer than 2 opportunity keywords found",
                opportunities=[],
            )

        body = _extract_body(prompt)
        sections = _split_sections(body)
        extracted: list[ExtractedOpportunity] = []
        for section in sections:
            eo = _section_to_opportunity(section)
            if eo is not None:
                extracted.append(eo)

        if not extracted:
            return ClassifierResult(
                is_opportunity=False,
                confidence=0.6,
                reasoning="stub: opportunity keywords but no parseable section",
            )

        return ClassifierResult(
            is_opportunity=True,
            confidence=0.9,
            reasoning=f"stub: extracted {len(extracted)} opportunity section(s)",
            opportunities=extracted,
        )

    @staticmethod
    def _write_copy(prompt: str, schema: type[T]) -> T:
        # The prompt uses "First name:" after pseudonymisation.
        student_name = _field_value(prompt, "First name:") or _field_value(prompt, "Name:")
        opp_title = _field_value(prompt, "Title:")
        opp_deadline = _field_value(prompt, "Deadline:")
        opp_org = _field_value(prompt, "Organisation:")
        opp_url = _field_value(prompt, "Link:")
        reasons = prompt.split("MATCH REASONS", 1)[1].strip() if "MATCH REASONS" in prompt else ""

        first_name = (student_name or "there").split()[0]
        deadline_line = (
            f" The deadline is {opp_deadline}."
            if opp_deadline and "not stated" not in opp_deadline
            else ""
        )
        url_line = f"\n\nApply here: {opp_url}" if opp_url and "(none" not in opp_url else ""
        reason_line = reasons.splitlines()[0].lstrip("- ").strip() if reasons else "your profile"

        title = opp_title or "This opportunity"
        org = opp_org or "the host"
        show_url = bool(opp_url) and "(none" not in (opp_url or "")
        subject = f"{opp_title or 'An opportunity'} — thought of you"[:78]
        body_text = (
            f"Hi {first_name},\n\n"
            f"Sharing this one because {reason_line}. "
            f"{title} from {org} looks like a strong fit.{deadline_line}"
            f"{url_line}\n\n"
            "If you've already applied, just ignore this — otherwise let me "
            "know if you want help on the app.\n\n"
            "— The Opportunities Team"
        )
        link_html = f'<p><a href="{opp_url}">Apply here</a></p>' if show_url else ""
        body_html = (
            f"<p>Hi {first_name},</p>"
            f"<p>Sharing this one because {reason_line}. <strong>{title}</strong> "
            f"from {org} looks like a strong fit.{deadline_line}</p>"
            f"{link_html}"
            "<p>— The Opportunities Team</p>"
        )
        return schema.model_validate(
            {"subject": subject, "body_text": body_text, "body_html": body_html}
        )


# --------------------------------------------------------------------------- #
# Parsing helpers                                                             #
# --------------------------------------------------------------------------- #


def _extract_body(prompt: str) -> str:
    """Pull the email body out of the classifier prompt template."""
    marker = "--- EMAIL BODY ---"
    if marker in prompt:
        return prompt.split(marker, 1)[1].split("--- END ---", 1)[0]
    return prompt


def _split_sections(body: str) -> list[str]:
    # If numbered sections exist, split on them; otherwise treat body as one.
    matches = list(_SECTION_RE.finditer(body))
    if len(matches) <= 1:
        return [body.strip()]
    sections: list[str] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections.append(body[start:end].strip())
    return [s for s in sections if s]


def _section_to_opportunity(section: str) -> ExtractedOpportunity | None:
    lines = [ln.strip() for ln in section.splitlines() if ln.strip()]
    if not lines:
        return None
    title = lines[0].split("—")[0].split(" - ")[0].strip().rstrip(":")
    if len(title) > 120 or len(title) < 4:
        return None

    lower = section.lower()
    kind = _detect_kind(lower)
    min_level = _detect_level(lower)
    fields = [f for f in _FIELD_KEYWORDS if f in lower]

    url_match = _URL_RE.search(section)
    deadline_iso = _detect_deadline(section)
    organization = _detect_org(section, title)
    summary = _first_two_sentences(section)

    return ExtractedOpportunity(
        title=title,
        kind=kind,
        organization=organization,
        summary=summary,
        eligibility="",
        deadline_iso=deadline_iso,
        url=url_match.group(0) if url_match else None,
        location="",
        tags=_detect_tags(lower),
        fields_of_study=fields,
        min_level=min_level,
    )


def _detect_kind(lower: str) -> OpportunityKind:
    for kind, keywords in _KIND_KEYWORDS.items():
        if any(k in lower for k in keywords):
            return kind
    return OpportunityKind.OTHER


def _detect_level(lower: str) -> StudentLevel:
    for level, keywords in _LEVEL_KEYWORDS.items():
        if any(k in lower for k in keywords):
            return level
    return StudentLevel.OTHER


def _detect_deadline(text: str) -> str | None:
    iso = _ISO_DATE_RE.search(text)
    if iso:
        return f"{iso.group(1)}-{iso.group(2)}-{iso.group(3)}"
    m = _DATE_RE.search(text)
    if not m:
        return None
    month = _MONTHS.get(m.group("month")[:3].lower())
    day = int(m.group("day"))
    year = int(m.group("year"))
    if not month:
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _detect_org(section: str, title: str) -> str:
    # Rough heuristic: phrases like "[Org Name] is accepting" or "by [Org Name]"
    patterns = [
        r"by\s+([A-Z][\w &.'-]{2,60})",
        r"from\s+([A-Z][\w &.'-]{2,60})",
        r"([A-Z][\w &.'-]{2,60})\s+is\s+(?:accepting|offering|hosting|running)",
    ]
    for pat in patterns:
        m = re.search(pat, section)
        if m:
            return m.group(1).strip(" .,")
    # Fallback: strip parenthetical bit from title.
    paren = re.search(r"\(([^)]+)\)", title)
    if paren:
        return paren.group(1).strip()
    return title[:60]


_TAG_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("open-source", ("open source", "open-source", "github")),
    ("women-in-stem", ("women", "girls who code", "women-in-stem", "non-binary")),
    ("remote", ("remote", "virtual", "online")),
    ("international", ("international", "worldwide", "global")),
    ("paid", ("paid", "stipend", "salary", "compensated")),
    ("free", ("free ", "no cost", "tuition-free")),
    ("hackathons", ("hackathon", "hack the north", "mlh", "devpost")),
    ("ai", ("artificial intelligence", " ai ", "ai/ml", "generative ai")),
    ("machine-learning", ("machine learning", "deep learning", "ml engineer")),
    ("space", ("space", "nasa", "jpl", "spacecraft", "aerospace")),
    ("robotics", ("robotics", "frc", "first robotics", "robot")),
    ("competitions", ("competition", "contest", "olympiad", "science fair")),
    ("science-fair", ("science fair",)),
    ("entrepreneurship", ("entrepreneur", "startup", "venture")),
)


def _detect_tags(lower: str) -> list[str]:
    tags: list[str] = []
    for tag, keywords in _TAG_RULES:
        if any(k in lower for k in keywords):
            tags.append(tag)
    return tags


def _first_two_sentences(section: str) -> str:
    clean = re.sub(r"\s+", " ", section).strip()
    parts = re.split(r"(?<=[.!?])\s+", clean)
    return " ".join(parts[:2])[:400]


def _field_value(prompt: str, label: str) -> str:
    for line in prompt.splitlines():
        if label in line:
            return line.split(label, 1)[1].strip()
    return ""


# --------------------------------------------------------------------------- #
# Stub Inkbox                                                                 #
# --------------------------------------------------------------------------- #


class StubInkbox(InkboxClient):
    """No-network Inkbox client. Logs all sends; inbound comes from .inbound_queue."""

    def __init__(self) -> None:
        settings = get_settings()
        self._settings = settings
        self._log_path = Path(settings.local_data_dir) / "sent.jsonl"
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.inbound_queue: list[InboundMessage] = []

    @property
    def log_path(self) -> Path:
        return self._log_path

    def send(  # type: ignore[override]
        self,
        *,
        to: list[str],
        subject: str,
        body_text: str,
        body_html: str | None = None,
        in_reply_to_message_id: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
    ) -> str:
        message_id = f"stub_{uuid.uuid4().hex[:12]}"
        record = {
            "id": message_id,
            "to": to,
            "cc": cc,
            "bcc": bcc,
            "subject": subject,
            "body_text": body_text,
            "body_html": body_html,
            "in_reply_to_message_id": in_reply_to_message_id,
            "sent_at": datetime.now(UTC).isoformat(),
        }
        with self._lock, self._log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        logger.bind(to=to, subject=subject, id=message_id).info("stub_inkbox.sent")
        return message_id

    def iter_inbound(self) -> Iterable[InboundMessage]:  # type: ignore[override]
        yield from self.inbound_queue

    def iter_unread_inbound(self) -> Iterable[InboundMessage]:  # type: ignore[override]
        yield from list(self.inbound_queue)
        self.inbound_queue.clear()

    def mark_read(self, message_ids: list[str]) -> None:  # type: ignore[override]
        return None

    def fetch(self, message_id: str) -> InboundMessage | None:  # type: ignore[override]
        for m in self.inbound_queue:
            if m.id == message_id:
                return m
        return None


__all__ = ["StubGemini", "StubInkbox"]
