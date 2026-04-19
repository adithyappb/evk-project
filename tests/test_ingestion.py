"""End-to-end ingestion pipeline tests."""

from __future__ import annotations

from evk.agents.classifier import ClassifierAgent
from evk.agents.ingestion import IngestionAgent
from evk.agents.personalizer import PersonalizerAgent
from evk.inkbox_client import InboundMessage
from evk.models import (
    ClassifierResult,
    ExtractedOpportunity,
    OpportunityKind,
    RawEmailStatus,
    StudentLevel,
)


def _inbound(body: str = "body", mid: str = "msg_1") -> InboundMessage:
    return InboundMessage(
        id=mid,
        rfc_message_id=f"<{mid}@inkboxmail.com>",
        thread_id="th_1",
        from_address="news@example.com",
        subject="STEM digest",
        body_text=body,
        body_html="",
        raw=None,
    )


def _build_ingestion(fake_repos, fake_inkbox, fake_gemini) -> IngestionAgent:
    classifier = ClassifierAgent(gemini=fake_gemini)
    personalizer = PersonalizerAgent(repos=fake_repos, gemini=fake_gemini, match_threshold=0.0)
    return IngestionAgent(
        repos=fake_repos,
        inkbox=fake_inkbox,
        classifier=classifier,
        personalizer=personalizer,
    )


def test_ingestion_skip_non_opportunity(fake_repos, fake_inkbox, fake_gemini):
    fake_gemini.queue_structured(
        ClassifierResult(is_opportunity=False, confidence=0.9, reasoning="marketing")
    )
    agent = _build_ingestion(fake_repos, fake_inkbox, fake_gemini)
    raw = agent.handle_inbound(_inbound())
    assert raw.status is RawEmailStatus.SKIPPED
    assert fake_repos.opportunities.list_all() == []
    assert fake_repos.drafts.list_all() == []


def test_ingestion_full_pipeline(fake_repos, fake_inkbox, fake_gemini, student_undergrad):
    fake_repos.students.upsert(student_undergrad)
    # 1) classifier extracts one opportunity
    extracted = ExtractedOpportunity(
        title="GSoC 2026",
        kind=OpportunityKind.INTERNSHIP,
        organization="Google OSS",
        summary="paid OSS internship",
        deadline_iso="2026-03-18",
        url="https://summerofcode.withgoogle.com/",
        tags=["open-source"],
        fields_of_study=["computer science"],
        min_level=StudentLevel.UNDERGRAD,
    )
    fake_gemini.queue_structured(
        ClassifierResult(
            is_opportunity=True,
            confidence=0.95,
            reasoning="clear",
            opportunities=[extracted],
        )
    )
    # 2) personalizer: queue one copy response per matching student
    fake_gemini.queue_structured(
        {
            "subject": "GSoC — perfect fit for you",
            "body_text": "Hey Ana, ...",
            "body_html": "<p>Hey Ana, ...</p>",
        }
    )

    agent = _build_ingestion(fake_repos, fake_inkbox, fake_gemini)
    raw = agent.handle_inbound(_inbound())

    assert raw.status is RawEmailStatus.CLASSIFIED
    assert len(raw.extracted_opportunity_ids) == 1
    opps = fake_repos.opportunities.list_all()
    assert len(opps) == 1
    drafts = fake_repos.drafts.list_all()
    assert len(drafts) == 1
    assert drafts[0].student_id == student_undergrad.id


def test_ingestion_idempotent_on_duplicate_message(fake_repos, fake_inkbox, fake_gemini):
    fake_gemini.queue_structured(
        ClassifierResult(is_opportunity=False, confidence=0.9, reasoning="skip")
    )
    agent = _build_ingestion(fake_repos, fake_inkbox, fake_gemini)
    msg = _inbound()
    agent.handle_inbound(msg)
    # second call should be a no-op; DON'T queue another classifier response.
    agent.handle_inbound(msg)
    # only one classifier call means dedup worked
    assert len(fake_gemini.calls) == 1


def test_ingestion_classifier_failure_marks_failed(fake_repos, fake_inkbox, fake_gemini):
    fake_gemini.queue_structured(RuntimeError("gemini outage"))
    agent = _build_ingestion(fake_repos, fake_inkbox, fake_gemini)
    raw = agent.handle_inbound(_inbound())
    assert raw.status is RawEmailStatus.FAILED
    assert raw.classification_error is not None
    assert "outage" in raw.classification_error


def test_poll_unread_marks_read(fake_repos, fake_inkbox, fake_gemini):
    fake_inkbox.inbound_queue = [_inbound(mid="msg_a"), _inbound(mid="msg_b")]
    fake_gemini.queue_structured(
        ClassifierResult(is_opportunity=False, confidence=0.9, reasoning="skip")
    )
    fake_gemini.queue_structured(
        ClassifierResult(is_opportunity=False, confidence=0.9, reasoning="skip")
    )
    agent = _build_ingestion(fake_repos, fake_inkbox, fake_gemini)
    processed, pending = agent.poll_unread()
    assert len(processed) == 2
    assert pending == []
    assert set(fake_inkbox.marked_read) == {"msg_a", "msg_b"}
