"""Classifier agent tests."""

from __future__ import annotations

from datetime import UTC, datetime

from evk.agents.classifier import ClassifierAgent, to_opportunity
from evk.models import (
    ClassifierResult,
    ExtractedOpportunity,
    OpportunityKind,
    RawEmail,
    StudentLevel,
)


def _raw_email(body: str = "body", subject: str = "subj") -> RawEmail:
    return RawEmail(
        id="r1",
        inkbox_message_id="r1",
        from_address="news@example.com",
        subject=subject,
        body_text=body,
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_classifier_returns_not_opportunity(fake_gemini):
    fake_gemini.queue_structured(
        ClassifierResult(is_opportunity=False, confidence=0.95, reasoning="marketing")
    )
    agent = ClassifierAgent(gemini=fake_gemini)
    result = agent.classify(_raw_email())
    assert result.is_opportunity is False
    assert result.opportunities == []


def test_classifier_extracts_opportunity(fake_gemini):
    extracted = ExtractedOpportunity(
        title="NASA Internship",
        kind=OpportunityKind.INTERNSHIP,
        organization="NASA",
        summary="Paid 10-week program.",
        deadline_iso="2026-02-28",
        url="https://nasa.gov/intern",
        tags=["space"],
        fields_of_study=["aerospace"],
        min_level=StudentLevel.UNDERGRAD,
    )
    fake_gemini.queue_structured(
        ClassifierResult(
            is_opportunity=True,
            confidence=0.98,
            reasoning="clear call to apply",
            opportunities=[extracted],
        )
    )
    result = ClassifierAgent(gemini=fake_gemini).classify(_raw_email())
    assert result.is_opportunity
    assert len(result.opportunities) == 1
    assert result.opportunities[0].title == "NASA Internship"


def test_to_opportunity_assigns_stable_id_and_eod_deadline():
    raw = _raw_email()
    extracted = ExtractedOpportunity(
        title="Grant X",
        kind=OpportunityKind.GRANT,
        organization="Org",
        summary="s",
        deadline_iso="2026-03-01",
        url="https://example.com/x",
    )
    opp1 = to_opportunity(extracted, source_raw_email=raw)
    opp2 = to_opportunity(extracted, source_raw_email=raw)
    assert opp1.id == opp2.id  # deterministic
    assert opp1.deadline == datetime(2026, 3, 1, 23, 59, 59, tzinfo=UTC)


def test_to_opportunity_handles_missing_deadline():
    raw = _raw_email()
    extracted = ExtractedOpportunity(
        title="Ongoing Program",
        kind=OpportunityKind.PROGRAM,
        organization="Org",
        summary="s",
        deadline_iso=None,
    )
    opp = to_opportunity(extracted, source_raw_email=raw)
    assert opp.deadline is None


def test_to_opportunity_drops_invalid_url():
    raw = _raw_email()
    extracted = ExtractedOpportunity(
        title="T",
        kind=OpportunityKind.JOB,
        organization="Org",
        summary="s",
        url="not a url at all",
    )
    opp = to_opportunity(extracted, source_raw_email=raw)
    assert opp.url is None


def test_to_opportunity_lowercases_tags_and_fields():
    raw = _raw_email()
    extracted = ExtractedOpportunity(
        title="T",
        kind=OpportunityKind.HACKATHON,
        organization="Org",
        summary="s",
        tags=["AI ", "Open-Source"],
        fields_of_study=["  Computer Science "],
    )
    opp = to_opportunity(extracted, source_raw_email=raw)
    assert opp.tags == ["ai", "open-source"]
    assert opp.fields_of_study == ["computer science"]


def test_classifier_bad_deadline_string_returns_none():
    raw = _raw_email()
    extracted = ExtractedOpportunity(
        title="T",
        kind=OpportunityKind.OTHER,
        organization="Org",
        summary="s",
        deadline_iso="not-a-date",
    )
    opp = to_opportunity(extracted, source_raw_email=raw)
    assert opp.deadline is None
