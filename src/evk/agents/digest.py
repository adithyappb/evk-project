"""Weekly digest agent — one email per student with their top-N matches.

Differences from the per-opportunity personaliser:

* **Grouped by student**, not by opportunity — one email, not N.
* **No Gemini call** — the digest is assembled from deterministic copy via
  Jinja2, so there's zero token cost and the output is audit-friendly.
* **Same approval/queue path** as other drafts — a digest is just a
  ``DraftMessage`` whose ``opportunity_id`` is ``digest:<iso_week>``.
* **Respects opt-in** and is **idempotent per ISO-week**.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from evk.agents.personalizer import Match, score_match
from evk.factory import get_repos
from evk.firestore_repo import Repos
from evk.logging import logger
from evk.models import DraftMessage, DraftStatus, Opportunity, Student

_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "ui" / "templates" / "digest"


@dataclass(frozen=True, slots=True)
class StudentDigest:
    """A rendered digest for one student — top picks + email payload."""

    student: Student
    picks: list[tuple[Match, Opportunity]]
    week_key: str
    subject: str
    body_text: str
    body_html: str


class DigestAgent:
    """Compile + queue a weekly digest per opted-in student."""

    def __init__(
        self,
        *,
        repos: Repos | None = None,
        top_n: int = 5,
        min_score: float = 0.5,
    ) -> None:
        self._repos = repos or get_repos()
        self._top_n = top_n
        self._min_score = min_score
        self._env = Environment(
            loader=FileSystemLoader(_TEMPLATE_DIR),
            autoescape=select_autoescape(["html"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    # ---- public -----------------------------------------------------------

    def build_and_queue(self, *, when: datetime | None = None) -> list[DraftMessage]:
        """Build a digest draft per opted-in student. Idempotent per ISO-week."""
        now = when or datetime.now(UTC)
        week_key = _week_key(now)
        opp_id = f"digest:{week_key}"

        students = self._repos.students.list_opted_in()
        available = self._available_opportunities(now=now)
        drafts: list[DraftMessage] = []

        for student in students:
            picks = self._rank(student, available)
            if not picks:
                logger.bind(student_id=student.id, week=week_key).info("digest.no_matches")
                continue
            draft_id = f"{opp_id}_{student.id}"
            if self._repos.drafts.get(draft_id) is not None:
                logger.bind(draft_id=draft_id).info("digest.already_queued")
                continue
            digest = self._render(student=student, picks=picks, week_key=week_key)
            drafts.append(_draft_from_digest(draft_id, opp_id, digest))

        if drafts:
            self._repos.drafts.upsert_many(drafts)
        logger.bind(week=week_key, drafts=len(drafts)).info("digest.queued")
        return drafts

    def build_one(self, student_id: str) -> StudentDigest | None:
        """Render (but don't persist) a digest for a single student — for previews."""
        student = self._repos.students.get(student_id)
        if student is None or not student.opted_in:
            return None
        picks = self._rank(student, self._available_opportunities())
        if not picks:
            return None
        return self._render(
            student=student,
            picks=picks,
            week_key=_week_key(datetime.now(UTC)),
        )

    # ---- internals --------------------------------------------------------

    def _available_opportunities(self, *, now: datetime | None = None) -> list[Opportunity]:
        """Future-dated or rolling opportunities only."""
        current = now or datetime.now(UTC)
        return [
            o
            for o in self._repos.opportunities.list_all()
            if o.deadline is None or o.deadline >= current
        ]

    def _rank(self, student: Student, opps: list[Opportunity]) -> list[tuple[Match, Opportunity]]:
        scored = [(m, o) for o in opps if (m := score_match(student, o)).score >= self._min_score]
        scored.sort(key=lambda pair: pair[0].score, reverse=True)
        return scored[: self._top_n]

    def _render(
        self,
        *,
        student: Student,
        picks: list[tuple[Match, Opportunity]],
        week_key: str,
    ) -> StudentDigest:
        ctx = {
            "student": student,
            "picks": picks,
            "week_key": week_key,
            "generated_at": datetime.now(UTC).strftime("%B %d, %Y"),
        }
        body_html = self._env.get_template("digest_email.html").render(**ctx)
        body_text = self._env.get_template("digest_email.txt").render(**ctx)
        subject = f"Your EVkids picks this week — {len(picks)} opportunit" + (
            "y" if len(picks) == 1 else "ies"
        )
        return StudentDigest(
            student=student,
            picks=picks,
            week_key=week_key,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
        )


# --------------------------------------------------------------------------- #
# Module-private helpers                                                      #
# --------------------------------------------------------------------------- #


def _week_key(when: datetime) -> str:
    y, w, _ = when.isocalendar()
    return f"{y}W{w:02d}"


def _draft_from_digest(draft_id: str, opp_id: str, digest: StudentDigest) -> DraftMessage:
    return DraftMessage(
        id=draft_id,
        student_id=digest.student.id,
        opportunity_id=opp_id,
        to_email=digest.student.email,
        subject=digest.subject,
        body_text=digest.body_text,
        body_html=digest.body_html,
        match_score=max(m.score for m, _ in digest.picks),
        match_reasons=[f"top {len(digest.picks)} picks for the week"],
        status=DraftStatus.PENDING_APPROVAL,
    )


__all__ = ["DigestAgent", "StudentDigest"]
