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

You receive the full body of an email sent to a shared inbox. Extract concrete,
student-applicable opportunities (scholarship, internship, hackathon, fellowship,
program, job, conference, competition) and flag anything uncertain for human review.

STRICT RULES — follow these exactly:

deadline_iso:
  - Set ONLY when the email explicitly states an APPLICATION or REGISTRATION deadline
    in plain text (e.g. "Apply by June 15", "Deadline: 2026-07-01", "Register by...").
  - Event dates, session dates, program start dates, and cohort dates are NOT
    application deadlines. If an email says "starts June 2" that is NOT a deadline.
  - If no explicit application deadline is stated: set deadline_iso to null.
  - NEVER infer or guess a deadline. When in doubt: null.

needs_review / review_reason:
  - Set needs_review=true and explain in review_reason whenever:
      * The deadline is unclear, missing, or you used an event date as a proxy.
      * Eligibility criteria are vague or not stated.
      * The opportunity may already be past (event date appears to have passed).
      * The URL is missing and you cannot verify the opportunity exists.
  - An admin will review these before they reach students — it is always better
    to flag than to guess.

Other rules:
  - Only set is_opportunity=true for things a student can actively apply to.
    Not general news, marketing, or funding for existing grantees.
  - Extract one entry per distinct opportunity (if an email has five, return five).
  - kind must be one of the enum values; use "other" only as last resort.
  - Never invent URLs; only copy links that appear verbatim in the email.
  - tags and fields_of_study must be short lowercase strings.
  - organization: the sponsoring org name exactly as stated; empty string if not named.
  - summary: 1-3 sentences describing what is on offer. Never invent details.
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
            max_output_tokens=8192,  # newsletters can contain many opportunities
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
    """Convert a Gemini-extracted opportunity into a canonical Opportunity record.

    Applies additional auto-flagging on top of whatever Gemini already set:
    - Past deadline → needs_review (admin decides whether to archive or update)
    - No deadline + no URL → needs_review (too little info to act on)
    """
    from evk.firestore_repo import OpportunityRepo

    deadline = _parse_deadline(extracted.deadline_iso)
    doc_id = OpportunityRepo.stable_id(
        title=extracted.title,
        deadline_iso=extracted.deadline_iso,
    )

    needs_review = extracted.needs_review
    review_reason = extracted.review_reason.strip()

    # Auto-flag: deadline in the past
    if deadline is not None and deadline < datetime.now(UTC):
        needs_review = True
        existing = f"{review_reason} | " if review_reason else ""
        review_reason = f"{existing}Deadline {deadline.date().isoformat()} appears to be in the past — confirm or archive."

    # Auto-flag: newsletter tracking/redirect URL (can't verify actual destination)
    _TRACKING_PATTERNS = ("/c/443/", "link.hello.boston.gov", "list-manage.com",
                          "/track/", "mailchimp.com/track", "click.mlsend")
    if extracted.url and any(p in extracted.url for p in _TRACKING_PATTERNS):
        needs_review = True
        existing = f"{review_reason} | " if review_reason else ""
        review_reason = (
            f"{existing}URL is a newsletter tracking redirect — please replace with the "
            "direct link to the opportunity and confirm it is still open."
        )

    # Auto-flag: no deadline AND no URL (admin can't verify or act on this)
    if deadline is None and not extracted.url:
        needs_review = True
        existing = f"{review_reason} | " if review_reason else ""
        review_reason = f"{existing}No deadline and no URL — admin should verify this opportunity is still open."

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
        needs_review=needs_review,
        review_reason=review_reason,
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
