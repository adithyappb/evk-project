"""Personalisation + approval agent.

For each new opportunity, score-match every opted-in student. For matches
above threshold, draft a short personalised email and store it as
`DraftMessage` with `status=pending_approval`. Nothing is sent from here.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from evk.factory import get_gemini, get_repos
from evk.firestore_repo import Repos
from evk.gemini_client import GeminiClient
from evk.logging import logger
from evk.models import (
    DraftMessage,
    DraftStatus,
    Opportunity,
    Student,
    StudentLevel,
)
from evk.privacy import pseudonymise

# Minimum match score to actually draft a message.
DEFAULT_MATCH_THRESHOLD = 0.45

_LEVEL_RANK = {
    StudentLevel.HIGH_SCHOOL: 0,
    StudentLevel.UNDERGRAD: 1,
    StudentLevel.GRAD: 2,
    StudentLevel.OTHER: 0,
}


# --------------------------------------------------------------------------- #
# Match scoring — cheap rule-based filter before we spend Gemini tokens.      #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Match:
    student: Student
    score: float
    reasons: list[str]


def score_match(student: Student, opp: Opportunity) -> Match:
    """Hybrid rule-based score in [0, 1] with human-readable reasons."""
    reasons: list[str] = []
    score = 0.0

    # 1. Level gating — if student is below required level, it's not a match.
    if _LEVEL_RANK[student.level] < _LEVEL_RANK[opp.min_level]:
        return Match(student=student, score=0.0, reasons=["student below minimum level"])

    # 2. Field-of-study overlap.
    student_fields = {f.lower() for f in student.fields_of_study}
    opp_fields = {f.lower() for f in opp.fields_of_study}
    if opp_fields:
        overlap = student_fields & opp_fields
        if overlap:
            score += 0.45
            reasons.append(f"field match: {', '.join(sorted(overlap))}")
        elif student_fields:
            # Opportunity is field-specific and student has unrelated fields.
            score += 0.05
    else:
        # Field-agnostic opportunity — mild positive signal.
        score += 0.2

    # 3. Interest / tag overlap.
    student_interests = {i.lower() for i in student.interests}
    opp_tags = {t.lower() for t in opp.tags}
    tag_overlap = student_interests & opp_tags
    if tag_overlap:
        score += min(0.45, 0.18 * len(tag_overlap))
        reasons.append(f"interests match: {', '.join(sorted(tag_overlap))}")

    # 4. Location heuristic (non-blocking).
    if (
        opp.location
        and student.location
        and (opp.location.lower() in student.location.lower() or "remote" in opp.location.lower())
    ):
        score += 0.1
        reasons.append("location compatible")

    # 5. Kind-specific bumps for common student targets.
    if opp.kind.value in {"scholarship", "internship", "hackathon", "fellowship"}:
        score += 0.1

    score = min(score, 1.0)
    if not reasons:
        reasons.append("weak match — no strong signals")
    return Match(student=student, score=round(score, 3), reasons=reasons)


# --------------------------------------------------------------------------- #
# Personalised copywriting via Gemini                                         #
# --------------------------------------------------------------------------- #


class _PersonalisedCopy(BaseModel):
    """Structured personalised-email output."""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(description="Personalised subject line, under 80 chars.")
    body_text: str = Field(description="Plain-text email body. Friendly, concise, 90-140 words.")
    body_html: str = Field(
        description=(
            "HTML version of the same body, simple tags only (p, a, strong, br, ul, li). "
            "No styles, no images."
        )
    )


_COPY_SYSTEM = """\
You are a thoughtful advisor writing one-to-one emails from a university's
opportunities desk to a specific student. You get:
- the student's profile
- the opportunity
- the reasons we think it fits

Write a short, warm, non-salesy email in second person ("you"):
- Open by name and mention ONE reason this is a fit based on their profile.
- Summarise the opportunity in 1-2 sentences (what, org, deadline if known).
- Give one concrete next step (apply link or where to find it).
- Sign off "— The Opportunities Team".
- Never fabricate details; only use what's provided.
- Body must be 90-140 words, plain text version first.
"""


class PersonalizerAgent:
    """Score students, draft personalised messages, persist as pending approvals."""

    def __init__(
        self,
        *,
        repos: Repos | None = None,
        gemini: GeminiClient | None = None,
        match_threshold: float = DEFAULT_MATCH_THRESHOLD,
    ) -> None:
        self._repos = repos or get_repos()
        self._gemini = gemini or get_gemini()
        self._threshold = match_threshold

    # --- public ------------------------------------------------------------

    def draft_for_opportunity(self, opp: Opportunity) -> list[DraftMessage]:
        """Create pending-approval drafts for every matching opted-in student."""
        students = self._repos.students.list_opted_in()
        drafts: list[DraftMessage] = []
        for student in students:
            match = score_match(student, opp)
            if match.score < self._threshold:
                logger.bind(student_id=student.id, opp_id=opp.id, score=match.score).debug(
                    "personalizer.skip"
                )
                continue
            if self._repos.drafts.exists_for_pair(student.id, opp.id):
                logger.bind(student_id=student.id, opp_id=opp.id).debug(
                    "personalizer.existing_draft"
                )
                continue
            draft = self._build_draft(match=match, opp=opp)
            self._repos.drafts.upsert(draft)
            drafts.append(draft)
            logger.bind(
                student_id=student.id,
                opp_id=opp.id,
                score=match.score,
                draft_id=draft.id,
            ).info("personalizer.draft_created")
        return drafts

    def draft_for_opportunities(self, opps: list[Opportunity]) -> list[DraftMessage]:
        out: list[DraftMessage] = []
        for opp in opps:
            out.extend(self.draft_for_opportunity(opp))
        return out

    # --- internals ---------------------------------------------------------

    def _build_draft(self, *, match: Match, opp: Opportunity) -> DraftMessage:
        copy = self._write_copy(match=match, opp=opp)
        draft_id = f"{opp.id}_{match.student.id}"
        return DraftMessage(
            id=draft_id,
            student_id=match.student.id,
            opportunity_id=opp.id,
            to_email=match.student.email,
            subject=copy.subject.strip(),
            body_text=copy.body_text.strip(),
            body_html=copy.body_html.strip(),
            match_score=match.score,
            match_reasons=match.reasons,
            status=DraftStatus.PENDING_APPROVAL,
        )

    def _write_copy(self, *, match: Match, opp: Opportunity) -> _PersonalisedCopy:
        prompt = _render_copy_prompt(student=match.student, opp=opp, reasons=match.reasons)
        return self._gemini.generate_structured(
            prompt=prompt,
            schema=_PersonalisedCopy,
            system_instruction=_COPY_SYSTEM,
            temperature=0.6,
            max_output_tokens=1024,
        )


def _render_copy_prompt(*, student: Student, opp: Opportunity, reasons: list[str]) -> str:
    """Render the Gemini prompt — student PII is pseudonymised before leaving.

    PII stripped: full name (→ first name), raw id (→ salted hash), street/city
    (→ region), age/DOB (→ band). Email & full location never ship.
    """
    ps = pseudonymise(student)
    deadline_line = (
        f"Deadline: {opp.deadline.date().isoformat()}" if opp.deadline else "Deadline: not stated"
    )
    url_line = f"Link: {opp.url}" if opp.url else "Link: (none provided)"
    return (
        f"{ps.to_prompt_block()}\n\n"
        "OPPORTUNITY\n"
        f"- Title: {opp.title}\n"
        f"- Kind: {opp.kind.value}\n"
        f"- Organisation: {opp.organization}\n"
        f"- Summary: {opp.summary}\n"
        f"- Eligibility: {opp.eligibility or 'n/a'}\n"
        f"- Location: {opp.location or 'n/a'}\n"
        f"- {deadline_line}\n"
        f"- {url_line}\n\n"
        "MATCH REASONS\n- " + "\n- ".join(reasons)
    )


__all__ = ["Match", "PersonalizerAgent", "score_match"]
