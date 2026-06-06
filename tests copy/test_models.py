"""Domain-model round-trip + schema tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from evk.models import (
    ClassifierResult,
    DraftMessage,
    DraftStatus,
    ExtractedOpportunity,
    Opportunity,
    OpportunityKind,
    RawEmail,
    RawEmailStatus,
    Student,
    StudentLevel,
)


def test_student_roundtrip():
    s = Student(
        id="s1",
        name="A",
        email="a@example.com",
        level=StudentLevel.UNDERGRAD,
        fields_of_study=["cs"],
        interests=["ai"],
    )
    dumped = s.model_dump(mode="json")
    loaded = Student.model_validate(dumped)
    assert loaded == s
    assert loaded.level is StudentLevel.UNDERGRAD


def test_student_bad_email_rejected():
    with pytest.raises(Exception):
        Student(
            id="s1",
            name="A",
            email="not-an-email",
            level=StudentLevel.UNDERGRAD,
        )


def test_opportunity_url_coercion():
    opp = Opportunity(
        id="o1",
        title="X",
        kind=OpportunityKind.HACKATHON,
        organization="Org",
        summary="s",
        url="https://example.com/apply",  # type: ignore[arg-type]
    )
    # HttpUrl serialises to string cleanly
    assert str(opp.url).startswith("https://example.com")


def test_extracted_opportunity_schema_accepts_minimal():
    eo = ExtractedOpportunity(
        title="t",
        kind=OpportunityKind.SCHOLARSHIP,
        organization="org",
        summary="s",
    )
    assert eo.deadline_iso is None
    assert eo.tags == []
    assert eo.min_level is StudentLevel.OTHER


def test_classifier_result_shape():
    cr = ClassifierResult(
        is_opportunity=True,
        confidence=0.9,
        reasoning="clearly advertises",
        opportunities=[
            ExtractedOpportunity(
                title="t", kind=OpportunityKind.SCHOLARSHIP, organization="o", summary="s"
            )
        ],
    )
    assert cr.opportunities[0].title == "t"


def test_classifier_result_rejects_bad_confidence():
    with pytest.raises(Exception):
        ClassifierResult(
            is_opportunity=False,
            confidence=1.5,  # > 1.0
            reasoning="x",
        )


def test_draft_default_status_is_pending():
    d = DraftMessage(
        id="d1",
        student_id="s1",
        opportunity_id="o1",
        to_email="a@example.com",
        subject="hi",
        body_text="body",
        match_score=0.5,
    )
    assert d.status is DraftStatus.PENDING_APPROVAL


def test_raw_email_default_status():
    r = RawEmail(
        id="r1",
        inkbox_message_id="msg_1",
        from_address="x@x.com",
        subject="s",
    )
    assert r.status is RawEmailStatus.RECEIVED


def test_opportunity_serialises_deadline_as_iso_string():
    opp = Opportunity(
        id="o1",
        title="t",
        kind=OpportunityKind.JOB,
        organization="o",
        summary="s",
        deadline=datetime(2026, 5, 1, tzinfo=UTC),
    )
    data = opp.model_dump(mode="json")
    assert isinstance(data["deadline"], str)
    assert data["deadline"].startswith("2026-05-01")
