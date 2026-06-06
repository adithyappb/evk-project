"""Tests for the weekly digest agent."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from evk.agents.digest import DigestAgent
from evk.models import (
    DraftStatus,
    Opportunity,
    OpportunityKind,
    Student,
    StudentLevel,
)
from tests.fakes import build_fake_repos


def _student(**kw) -> Student:
    base = dict(
        id="student_ava",
        name="Ava Singh",
        email="ava@example.edu",
        level=StudentLevel.UNDERGRAD,
        school="",
        graduation_year=2027,
        fields_of_study=["computer science"],
        interests=["ai", "robotics"],
        location="Mumbai, India",
        bio="",
        opted_in=True,
    )
    base.update(kw)
    return Student(**base)


def _opp(id: str, title: str, **kw) -> Opportunity:
    base = dict(
        id=id,
        title=title,
        kind=OpportunityKind.INTERNSHIP,
        organization="Org",
        summary="Summary of " + title,
        eligibility="",
        deadline=datetime.now(UTC) + timedelta(days=30),
        url="https://example.com/apply",
        location="Remote",
        tags=["ai"],
        fields_of_study=["computer science"],
        min_level=StudentLevel.UNDERGRAD,
        source_raw_email_id="",
        source_subject="",
        source_sender="",
    )
    base.update(kw)
    return Opportunity(**base)


@pytest.fixture
def repos():
    r = build_fake_repos()
    r.students.upsert(_student())
    for i, title in enumerate(
        [
            "AI Research Internship 2026",
            "Robotics Fellowship 2026",
            "Design Hackathon 2026",
        ]
    ):
        r.opportunities.upsert(_opp(id=f"opp_{i}", title=title))
    return r


def test_digest_queues_one_draft_per_opted_in_student(repos):
    drafts = DigestAgent(repos=repos, top_n=3, min_score=0.0).build_and_queue()
    assert len(drafts) == 1
    d = drafts[0]
    assert d.status is DraftStatus.PENDING_APPROVAL
    assert d.opportunity_id.startswith("digest:")
    assert d.to_email == "ava@example.edu"


def test_digest_is_idempotent_per_iso_week(repos):
    agent = DigestAgent(repos=repos, top_n=3, min_score=0.0)
    agent.build_and_queue()
    second_run = agent.build_and_queue()
    assert second_run == []


def test_digest_skips_students_with_no_matches(repos):
    # Unrelated interests → scores below threshold
    repos.students.upsert(
        _student(
            id="student_bob",
            email="bob@example.edu",
            fields_of_study=["medieval history"],
            interests=["latin"],
        )
    )
    drafts = DigestAgent(repos=repos, top_n=3, min_score=0.5).build_and_queue()
    # Ava still matches; Bob does not
    to_emails = {d.to_email for d in drafts}
    assert "bob@example.edu" not in to_emails


def test_digest_excludes_past_deadlines(repos):
    repos.opportunities.upsert(
        _opp(id="past", title="Expired 2023", deadline=datetime.now(UTC) - timedelta(days=10))
    )
    ava = repos.students.get("student_ava")
    assert ava is not None
    preview = DigestAgent(repos=repos, top_n=5, min_score=0.0).build_one(ava.id)
    assert preview is not None
    assert all(o.deadline is None or o.deadline >= datetime.now(UTC) for _, o in preview.picks)


def test_digest_body_contains_student_first_name(repos):
    preview = DigestAgent(repos=repos, top_n=3, min_score=0.0).build_one("student_ava")
    assert preview is not None
    assert "Ava" in preview.body_text
    assert "Ava" in preview.body_html


def test_digest_respects_opt_out(repos):
    repos.students.upsert(_student(opted_in=False))
    drafts = DigestAgent(repos=repos).build_and_queue()
    assert drafts == []
