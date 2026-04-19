"""Classifier agent: decides whether an email is a student opportunity and extracts it.

Takes a `RawEmail`, returns a `ClassifierResult`. Pure function on top of the
Gemini client — no Firestore or Inkbox side-effects.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from evk.factory import get_gemini
from evk.gemini_client import GeminiClient
from evk.logging import logger
from evk.models import (
    ClassifierResult,
    ExtractedOpportunity,
    Opportunity,
    RawEmail,
)

_SYSTEM_INSTRUCTION = """\
You are an expert classifier for a student-opportunities newsletter pipeline.

You receive the full body of an email that was sent to a shared inbox. Decide
whether the email advertises one or more concrete, student-applicable
opportunities (scholarship, internship, hackathon, competition, fellowship,
conference, grant, program, or job) and, if so, extract each one.

Rules:
- Only set `is_opportunity=true` when there is a specific thing a student can
  apply to, not general news, funding announcements for existing grantees,
  marketing, or aggregator digests without dates/links.
- Extract one entry per distinct opportunity. If an email bundles five,
  return five entries.
- Dates must be ISO-8601 calendar dates (YYYY-MM-DD). If only a month/year is
  given, use the last day of that month. If truly unknown, use null.
- `kind` must be one of the enum values. Use `other` only as last resort.
- Never invent URLs; only copy links that appear in the email.
- `tags` and `fields_of_study` must be short lowercase strings.
- Be conservative: if in doubt, set is_opportunity=false and return an empty list.
"""


class ClassifierAgent:
    """Thin orchestration around Gemini structured output."""

    def __init__(self, gemini: GeminiClient | None = None) -> None:
        self._gemini = gemini or get_gemini()

    def classify(self, email: RawEmail) -> ClassifierResult:
        """Run the classifier on a raw email."""
        prompt = _build_prompt(email)
        logger.bind(raw_email_id=email.id, subject=email.subject, sender=email.from_address).info(
            "classifier.run"
        )
        result = self._gemini.generate_structured(
            prompt=prompt,
            schema=ClassifierResult,
            system_instruction=_SYSTEM_INSTRUCTION,
            temperature=0.1,
        )
        logger.bind(
            raw_email_id=email.id,
            is_opportunity=result.is_opportunity,
            n_opportunities=len(result.opportunities),
            confidence=result.confidence,
        ).info("classifier.done")
        return result


def _build_prompt(email: RawEmail) -> str:
    body = email.body_text.strip() or _strip_html(email.body_html)
    body = body[:20_000]  # hard cap; Gemini handles plenty but keep tokens sane
    return (
        f"FROM: {email.from_address}\n"
        f"SUBJECT: {email.subject}\n"
        f"DATE: {email.received_at.isoformat()}\n"
        f"--- EMAIL BODY ---\n{body}\n--- END ---"
    )


def _strip_html(html: str) -> str:
    # Deliberately minimal — Gemini tolerates messy input.
    import re

    text = re.sub(r"<style.*?</style>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# --------------------------------------------------------------------------- #
# Mapping extracted → canonical Opportunity                                   #
# --------------------------------------------------------------------------- #


def to_opportunity(
    extracted: ExtractedOpportunity,
    *,
    source_raw_email: RawEmail,
) -> Opportunity:
    """Convert a Gemini-extracted opportunity into a canonical Opportunity record."""
    from evk.firestore_repo import OpportunityRepo

    deadline = _parse_deadline(extracted.deadline_iso)
    doc_id = OpportunityRepo.stable_id(
        title=extracted.title,
        deadline_iso=extracted.deadline_iso,
    )
    return Opportunity(
        id=doc_id,
        title=extracted.title.strip(),
        kind=extracted.kind,
        organization=extracted.organization.strip(),
        summary=extracted.summary.strip(),
        eligibility=extracted.eligibility.strip(),
        deadline=deadline,
        url=_safe_url(extracted.url),
        location=extracted.location.strip(),
        tags=[t.lower().strip() for t in extracted.tags if t.strip()],
        fields_of_study=[f.lower().strip() for f in extracted.fields_of_study if f.strip()],
        min_level=extracted.min_level,
        source_raw_email_id=source_raw_email.id,
        source_subject=source_raw_email.subject,
        source_sender=source_raw_email.from_address,
    )


def _safe_url(url: str | None) -> str | None:
    """Return the URL if it validates as an HttpUrl, else None. Never raises."""
    if not url:
        return None
    from pydantic import HttpUrl, ValidationError
    from pydantic_core import PydanticCustomError

    try:
        HttpUrl(url)
    except (ValidationError, PydanticCustomError, ValueError):
        logger.bind(url=url).warning("classifier.invalid_url_dropped")
        return None
    return url


def _parse_deadline(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        try:
            return datetime.fromisoformat(iso).astimezone(UTC)
        except ValueError:
            logger.bind(value=iso).warning("classifier.bad_deadline")
            return None
    # Treat dates as end-of-day UTC so reminders fire correctly.
    return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=UTC)


__all__ = ["ClassifierAgent", "to_opportunity"]
