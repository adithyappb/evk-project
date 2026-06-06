"""StubGemini + StubInkbox behaviour."""

from __future__ import annotations

import json

from evk.agents.classifier import ClassifierAgent
from evk.agents.ingestion import IngestionAgent
from evk.agents.personalizer import PersonalizerAgent
from evk.inkbox_client import InboundMessage
from evk.models import ClassifierResult, OpportunityKind, StudentLevel
from evk.stubs import StubGemini, StubInkbox


def test_stub_gemini_classifies_non_opportunity():
    stub = StubGemini()
    out = stub.generate_structured(
        prompt="Just a newsletter saying hello, nothing to apply to.",
        schema=ClassifierResult,
    )
    assert out.is_opportunity is False


def test_stub_gemini_classifies_opportunity_with_structure():
    stub = StubGemini()
    prompt = (
        "FROM: news@example.com\n"
        "SUBJECT: Weekly digest\n"
        "DATE: 2026-04-01T00:00:00+00:00\n"
        "--- EMAIL BODY ---\n"
        "1) The Gates Scholarship\n"
        "The Gates Scholarship is accepting applications from undergrad students. "
        "Apply by September 15, 2099 at https://gates.example/apply.\n"
        "--- END ---"
    )
    out = stub.generate_structured(prompt=prompt, schema=ClassifierResult)
    assert out.is_opportunity is True
    assert len(out.opportunities) == 1
    opp = out.opportunities[0]
    assert opp.kind == OpportunityKind.SCHOLARSHIP
    assert opp.min_level == StudentLevel.UNDERGRAD
    assert opp.deadline_iso == "2099-09-15"
    assert opp.url == "https://gates.example/apply"


def test_stub_gemini_classifier_agent_integration(fake_repos, fake_inkbox):
    """The stub plugs into the real ClassifierAgent end-to-end."""
    stub = StubGemini()
    agent = IngestionAgent(
        repos=fake_repos,
        inkbox=fake_inkbox,
        classifier=ClassifierAgent(gemini=stub),
        personalizer=PersonalizerAgent(repos=fake_repos, gemini=stub, match_threshold=0.0),
    )
    msg = InboundMessage(
        id="m1",
        rfc_message_id=None,
        thread_id=None,
        from_address="news@example.com",
        subject="Opportunities digest",
        body_text=(
            "1) MIT Summer Research Internship\n"
            "MIT is offering a paid summer internship for undergraduate students "
            "interested in computer science. Apply by April 30, 2099 at "
            "https://mit.example/apply."
        ),
        body_html="",
        raw=None,
    )
    raw = agent.handle_inbound(msg)
    assert raw.status.value == "classified"
    assert len(raw.extracted_opportunity_ids) == 1


def test_stub_inkbox_logs_sends(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    from evk.config import get_settings

    get_settings.cache_clear()
    ink = StubInkbox()
    mid = ink.send(to=["alice@example.com"], subject="hi", body_text="hello", body_html=None)
    assert mid.startswith("stub_")

    content = ink.log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(content) == 1
    record = json.loads(content[0])
    assert record["to"] == ["alice@example.com"]
    assert record["subject"] == "hi"
    assert record["id"] == mid


def test_stub_inkbox_inbound_drains_unread():
    ink = StubInkbox()
    ink.inbound_queue.append(
        InboundMessage(
            id="m1",
            rfc_message_id=None,
            thread_id=None,
            from_address="x@x",
            subject="s",
            body_text="b",
            body_html="",
            raw=None,
        )
    )
    unread = list(ink.iter_unread_inbound())
    assert len(unread) == 1
    # drained after read
    assert list(ink.iter_unread_inbound()) == []
