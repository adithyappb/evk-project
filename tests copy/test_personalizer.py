"""Personalizer: score_match rules + draft generation."""

from __future__ import annotations

from evk.agents.personalizer import (
    DEFAULT_MATCH_THRESHOLD,
    PersonalizerAgent,
    score_match,
)
from evk.models import DraftStatus, StudentLevel

# --------------------------------------------------------------------------- #
# Scoring rules                                                               #
# --------------------------------------------------------------------------- #


def test_below_min_level_is_zero(student_highschool, opp_hackathon):
    # hackathon requires undergrad, max is high-school
    match = score_match(student_highschool, opp_hackathon)
    assert match.score == 0.0


def test_field_overlap_scores_highly(student_undergrad, opp_hackathon):
    match = score_match(student_undergrad, opp_hackathon)
    # field match (0.45) + kind bonus for hackathon (0.05) = 0.50
    assert match.score >= 0.5
    assert any("field match" in r for r in match.reasons)


def test_interest_tag_overlap(student_highschool, opp_highschool_sciencefair):
    match = score_match(student_highschool, opp_highschool_sciencefair)
    assert match.score > 0.5
    assert any("interests match" in r for r in match.reasons)


def test_unrelated_student_scores_low(opp_hackathon):
    from evk.models import Student

    art_student = Student(
        id="s_art",
        name="A",
        email="a@example.com",
        level=StudentLevel.UNDERGRAD,
        fields_of_study=["art history"],
        interests=["painting"],
    )
    match = score_match(art_student, opp_hackathon)
    assert match.score < DEFAULT_MATCH_THRESHOLD


def test_score_clamped_to_one():
    from evk.models import Opportunity, OpportunityKind, Student

    s = Student(
        id="s",
        name="A",
        email="a@example.com",
        level=StudentLevel.UNDERGRAD,
        fields_of_study=["computer science"],
        interests=["ai", "ml", "nlp", "robotics", "open-source"],
        location="Remote",
    )
    o = Opportunity(
        id="o",
        title="Mega match",
        kind=OpportunityKind.HACKATHON,
        organization="Org",
        summary="s",
        tags=["ai", "ml", "nlp", "robotics", "open-source"],
        fields_of_study=["computer science"],
        location="remote",
        min_level=StudentLevel.UNDERGRAD,
    )
    assert score_match(s, o).score <= 1.0


# --------------------------------------------------------------------------- #
# Draft creation                                                              #
# --------------------------------------------------------------------------- #


def _queue_copy(fake_gemini, subject: str = "Subj", text: str = "body text"):
    """Queue a _PersonalisedCopy response."""
    fake_gemini.queue_structured(
        {"subject": subject, "body_text": text, "body_html": f"<p>{text}</p>"}
    )


def test_draft_created_for_matching_student(
    fake_repos, fake_gemini, student_undergrad, opp_hackathon
):
    fake_repos.students.upsert(student_undergrad)
    _queue_copy(fake_gemini, subject="You'd love Hack the North")
    agent = PersonalizerAgent(repos=fake_repos, gemini=fake_gemini, match_threshold=0.0)
    drafts = agent.draft_for_opportunity(opp_hackathon)
    assert len(drafts) == 1
    d = drafts[0]
    assert d.status is DraftStatus.PENDING_APPROVAL
    assert d.student_id == student_undergrad.id
    assert d.subject == "You'd love Hack the North"
    # persisted in repo
    assert fake_repos.drafts.get(d.id) is not None


def test_draft_skipped_below_threshold(fake_repos, fake_gemini, student_highschool, opp_hackathon):
    fake_repos.students.upsert(student_highschool)
    # no queued copy — we shouldn't even call Gemini
    agent = PersonalizerAgent(repos=fake_repos, gemini=fake_gemini, match_threshold=0.4)
    drafts = agent.draft_for_opportunity(opp_hackathon)
    assert drafts == []
    assert fake_gemini.calls == []


def test_draft_dedupes_on_repeat(fake_repos, fake_gemini, student_undergrad, opp_hackathon):
    fake_repos.students.upsert(student_undergrad)
    _queue_copy(fake_gemini)
    agent = PersonalizerAgent(repos=fake_repos, gemini=fake_gemini, match_threshold=0.0)
    first = agent.draft_for_opportunity(opp_hackathon)
    assert len(first) == 1
    second = agent.draft_for_opportunity(opp_hackathon)
    assert second == []  # no duplicate draft
    # Gemini only called once
    assert len(fake_gemini.calls) == 1


def test_opted_out_students_excluded(fake_repos, fake_gemini, student_undergrad, opp_hackathon):
    student_undergrad.opted_in = False
    fake_repos.students.upsert(student_undergrad)
    agent = PersonalizerAgent(repos=fake_repos, gemini=fake_gemini, match_threshold=0.0)
    drafts = agent.draft_for_opportunity(opp_hackathon)
    assert drafts == []
