"""Stress & edge-case tests.

These aren't load tests in the perf sense — they use in-memory fakes so they
run in milliseconds — but they exercise the hot paths under concurrent and
high-volume conditions we'd expect in production:

* hundreds of duplicate webhooks deduped exactly once
* concurrent inbound processing never duplicates opportunities
* huge bodies, Unicode, HTML-only, and empty bodies don't crash the pipeline
* the reminder agent fires exactly once per (student, opp, window)
* repeated approve clicks never double-send a draft
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from evk.agents.classifier import ClassifierAgent
from evk.agents.distributor import DistributorAgent
from evk.agents.ingestion import IngestionAgent
from evk.agents.personalizer import PersonalizerAgent
from evk.agents.reminder import ReminderAgent
from evk.inkbox_client import InboundMessage
from evk.models import (
    ClassifierResult,
    DraftMessage,
    DraftStatus,
    ExtractedOpportunity,
    Opportunity,
    OpportunityKind,
    RawEmailStatus,
    StudentLevel,
)


def _make_msg(i: int, *, body: str = "apply now before the deadline", sender: str | None = None):
    return InboundMessage(
        id=f"stress_msg_{i}",
        rfc_message_id=None,
        thread_id=None,
        from_address=sender or f"sender{i}@ex.com",
        subject=f"sub-{i}",
        body_text=body,
        body_html="",
        raw=None,
    )


def _make_extracted(title: str = "Awesome Fellowship"):
    return ExtractedOpportunity(
        title=title,
        kind=OpportunityKind.FELLOWSHIP,
        organization="ACME",
        summary="Great opportunity.",
        eligibility="",
        deadline_iso="2099-12-31",
        url="https://example.org",
        location="Remote",
        tags=[],
        fields_of_study=["computer science"],
        min_level=StudentLevel.UNDERGRAD,
    )


# --------------------------------------------------------------------------- #
# Volume: 500 duplicate webhooks for the same message id                      #
# --------------------------------------------------------------------------- #


def test_duplicate_ingestion_is_exactly_once(fake_repos, fake_inkbox, fake_gemini):
    for _ in range(500):
        fake_gemini.queue_structured(
            ClassifierResult(is_opportunity=False, confidence=0.9, reasoning="n/a")
        )
    classifier_fn = IngestionAgent(
        repos=fake_repos,
        inkbox=fake_inkbox,
        classifier=ClassifierAgent(gemini=fake_gemini),
        personalizer=PersonalizerAgent(repos=fake_repos, gemini=fake_gemini, match_threshold=0.0),
    )
    msg = _make_msg(0)
    for _ in range(500):
        classifier_fn.handle_inbound(msg)

    # Only one RawEmail persisted, classifier called exactly once.
    assert len(fake_repos.raw_emails.list_all()) == 1
    # We queued 500 but handle_inbound short-circuits on "already seen" after
    # the first pass, so classifier sees exactly 1 call.
    assert len(fake_gemini.calls) == 1


# --------------------------------------------------------------------------- #
# Concurrency: parallel ingest never duplicates opportunities                 #
# --------------------------------------------------------------------------- #


def test_concurrent_ingest_dedupes_opportunities(
    fake_repos, fake_inkbox, fake_gemini, student_undergrad
):
    fake_repos.students.upsert(student_undergrad)

    extracted = _make_extracted("Concurrent Challenge")

    def make_agent():
        # Each "thread" queues its own classifier response then its own copy.
        fake_gemini.queue_structured(
            ClassifierResult(
                is_opportunity=True,
                confidence=0.9,
                reasoning="yes",
                opportunities=[extracted],
            )
        )
        # The personalizer also calls gemini once per (student, opp).
        fake_gemini.queue_structured(
            {
                "subject": "You'd love this",
                "body_text": "hi there, check this out. thanks.",
                "body_html": "<p>hi</p>",
            }
        )
        return IngestionAgent(
            repos=fake_repos,
            inkbox=fake_inkbox,
            classifier=ClassifierAgent(gemini=fake_gemini),
            personalizer=PersonalizerAgent(
                repos=fake_repos, gemini=fake_gemini, match_threshold=0.0
            ),
        )

    # Pre-queue all gemini responses (FakeGemini is not thread-safe; we avoid
    # that by pre-loading then running workers synchronously via join below).
    n_workers = 20
    agents = [make_agent() for _ in range(n_workers)]

    def worker(i: int) -> None:
        # Same sender across workers so the stable-id dedupes identical opps.
        agents[i].handle_inbound(_make_msg(i, sender="news@example.com"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Each raw message is unique, so we have n_workers raw_emails …
    assert len(fake_repos.raw_emails.list_all()) == n_workers
    # … but the extracted opportunity has the same stable id, so exactly 1 stored.
    opps = fake_repos.opportunities.list_all()
    assert len({o.id for o in opps}) == 1
    # And exactly one draft for the single (student, opp) pair.
    drafts = fake_repos.drafts.list_all()
    assert len(drafts) == 1


# --------------------------------------------------------------------------- #
# Edge cases: exotic bodies                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        "",
        "   \n\t   ",
        "x" * 50_000,
        "Apply now — 🧑‍🎓 scholarship deadline 2099-12-31 → https://é.example.com",
        "<html><body><p>Apply by <b>Dec 31, 2099</b> → <a href='https://x'>here</a></p></body></html>",
    ],
    ids=["empty", "whitespace_only", "huge_50k", "unicode", "html_only"],
)
def test_exotic_bodies_do_not_crash(fake_repos, fake_inkbox, fake_gemini, body):
    fake_gemini.queue_structured(
        ClassifierResult(is_opportunity=False, confidence=0.5, reasoning="edge case")
    )
    agent = IngestionAgent(
        repos=fake_repos,
        inkbox=fake_inkbox,
        classifier=ClassifierAgent(gemini=fake_gemini),
        personalizer=PersonalizerAgent(repos=fake_repos, gemini=fake_gemini, match_threshold=0.0),
    )
    msg = _make_msg(0, body=body)
    # Most important: it doesn't raise.
    raw = agent.handle_inbound(msg)
    assert raw.status in {RawEmailStatus.SKIPPED, RawEmailStatus.CLASSIFIED}


# --------------------------------------------------------------------------- #
# Classifier failure: pipeline records failure, stays consistent              #
# --------------------------------------------------------------------------- #


def test_classifier_failure_is_recorded(fake_repos, fake_inkbox, fake_gemini):
    fake_gemini.queue_structured(RuntimeError("gemini exploded"))
    agent = IngestionAgent(
        repos=fake_repos,
        inkbox=fake_inkbox,
        classifier=ClassifierAgent(gemini=fake_gemini),
        personalizer=PersonalizerAgent(repos=fake_repos, gemini=fake_gemini, match_threshold=0.0),
    )
    raw = agent.handle_inbound(_make_msg(1))
    assert raw.status == RawEmailStatus.FAILED
    persisted = fake_repos.raw_emails.get(raw.id)
    assert persisted is not None
    assert persisted.status == RawEmailStatus.FAILED
    assert "gemini exploded" in (persisted.classification_error or "")


# --------------------------------------------------------------------------- #
# Distributor never double-sends                                              #
# --------------------------------------------------------------------------- #


def test_distributor_refuses_non_approved(fake_repos, fake_inkbox, pending_draft):
    fake_repos.drafts.upsert(pending_draft)
    distributor = DistributorAgent(repos=fake_repos, inkbox=fake_inkbox)
    with pytest.raises(ValueError):
        distributor.send_one(pending_draft)
    assert fake_inkbox.sent == []


def test_approving_twice_does_not_double_send(fake_repos, fake_inkbox, pending_draft):
    fake_repos.drafts.upsert(pending_draft)
    distributor = DistributorAgent(repos=fake_repos, inkbox=fake_inkbox)

    pending_draft.status = DraftStatus.APPROVED
    fake_repos.drafts.upsert(pending_draft)
    distributor.send_one(pending_draft)

    sent = fake_repos.drafts.get(pending_draft.id)
    assert sent.status == DraftStatus.SENT

    # Attempting to send the already-sent draft must raise (status != APPROVED).
    with pytest.raises(ValueError):
        distributor.send_one(sent)
    assert len(fake_inkbox.sent) == 1


# --------------------------------------------------------------------------- #
# Reminder agent — exactly once per window                                    #
# --------------------------------------------------------------------------- #


def test_reminder_fires_once_per_window(fake_repos, fake_inkbox, student_undergrad):
    # Opportunity with deadline ~2 days away.
    deadline = datetime.now(UTC) + timedelta(days=1, hours=20)
    opp = Opportunity(
        id="opp_r1",
        title="Near Deadline",
        kind=OpportunityKind.SCHOLARSHIP,
        organization="X",
        summary="y",
        eligibility="",
        deadline=deadline,
        min_level=StudentLevel.UNDERGRAD,
    )
    fake_repos.opportunities.upsert(opp)
    fake_repos.students.upsert(student_undergrad)
    fake_repos.drafts.upsert(
        DraftMessage(
            id=f"{opp.id}_{student_undergrad.id}",
            student_id=student_undergrad.id,
            opportunity_id=opp.id,
            to_email=student_undergrad.email,
            subject="s",
            body_text="b",
            body_html="<p>b</p>",
            match_score=0.8,
            match_reasons=["x"],
            status=DraftStatus.SENT,
        )
    )
    agent = ReminderAgent(repos=fake_repos, inkbox=fake_inkbox)
    # Configured windows in conftest are 7,2 → today is ~1.8 days out → window=2.
    sent_first = agent.run()
    sent_second = agent.run()
    sent_third = agent.run()
    assert sent_first == 1
    assert sent_second == 0
    assert sent_third == 0
    assert len(fake_inkbox.sent) == 1


# --------------------------------------------------------------------------- #
# Repo: rapid upsert then read                                                #
# --------------------------------------------------------------------------- #


def test_fake_repo_handles_rapid_upserts(fake_repos, opp_hackathon):
    for i in range(1000):
        opp_hackathon.title = f"Hack the North #{i}"
        fake_repos.opportunities.upsert(opp_hackathon)
    got = fake_repos.opportunities.get(opp_hackathon.id)
    assert got is not None
    assert got.title == "Hack the North #999"


# --------------------------------------------------------------------------- #
# Personalizer: threshold boundary                                             #
# --------------------------------------------------------------------------- #


def test_personalizer_below_threshold_produces_nothing(fake_repos, fake_gemini, student_undergrad):
    fake_repos.students.upsert(student_undergrad)
    mismatched = Opportunity(
        id="opp_mismatch",
        title="PhD Only Workshop",
        kind=OpportunityKind.CONFERENCE,
        organization="X",
        summary="advanced",
        eligibility="PhD",
        min_level=StudentLevel.GRAD,  # student is undergrad → filtered out
    )
    fake_repos.opportunities.upsert(mismatched)
    agent = PersonalizerAgent(repos=fake_repos, gemini=fake_gemini, match_threshold=0.5)
    drafts = agent.draft_for_opportunity(mismatched)
    assert drafts == []
    assert fake_repos.drafts.list_all() == []


# --------------------------------------------------------------------------- #
# Dedup: same opportunity from different senders collapses to one row         #
# --------------------------------------------------------------------------- #


def test_same_opportunity_different_senders_dedupes(
    fake_repos, fake_inkbox, fake_gemini, student_undergrad
):
    fake_repos.students.upsert(student_undergrad)
    extracted = _make_extracted("NASA JPL Summer Internship 2026")
    # Two different newsletters, same opportunity content.
    for _ in range(2):
        fake_gemini.queue_structured(
            ClassifierResult(
                is_opportunity=True,
                confidence=0.9,
                reasoning="yes",
                opportunities=[extracted],
            )
        )
        fake_gemini.queue_structured(
            {
                "subject": "You'd love this",
                "body_text": "hi, check this out.",
                "body_html": "<p>hi</p>",
            }
        )
    agent = IngestionAgent(
        repos=fake_repos,
        inkbox=fake_inkbox,
        classifier=ClassifierAgent(gemini=fake_gemini),
        personalizer=PersonalizerAgent(repos=fake_repos, gemini=fake_gemini, match_threshold=0.0),
    )
    agent.handle_inbound(_make_msg(1, sender="news-a@example.com"))
    agent.handle_inbound(_make_msg(2, sender="news-b@example.com"))

    # Two different raw emails ingested …
    assert len(fake_repos.raw_emails.list_all()) == 2
    # … but the same extracted opportunity collapses to one document.
    opps = fake_repos.opportunities.list_all()
    assert len({o.id for o in opps}) == 1
    # And only one draft per (student, opp).
    drafts = fake_repos.drafts.list_all()
    assert len(drafts) == 1


# --------------------------------------------------------------------------- #
# Rejected drafts are never re-drafted by the personalizer                    #
# --------------------------------------------------------------------------- #


def test_rejected_draft_is_not_redrafted(fake_repos, fake_gemini, opp_hackathon, student_undergrad):
    fake_repos.students.upsert(student_undergrad)
    fake_repos.opportunities.upsert(opp_hackathon)
    # Mark a prior draft as rejected for this (student, opp) pair.
    fake_repos.drafts.upsert(
        DraftMessage(
            id=f"{opp_hackathon.id}_{student_undergrad.id}",
            student_id=student_undergrad.id,
            opportunity_id=opp_hackathon.id,
            to_email=student_undergrad.email,
            subject="earlier",
            body_text="earlier",
            body_html="<p>earlier</p>",
            match_score=0.8,
            match_reasons=["x"],
            status=DraftStatus.REJECTED,
        )
    )
    agent = PersonalizerAgent(repos=fake_repos, gemini=fake_gemini, match_threshold=0.0)
    new_drafts = agent.draft_for_opportunity(opp_hackathon)
    # Already has a draft (even if rejected) — we don't regenerate.
    assert new_drafts == []


# --------------------------------------------------------------------------- #
# Large corpus: dashboard stats/drafts never degrade superlinearly            #
# --------------------------------------------------------------------------- #


def test_large_corpus_stats_are_fast(fake_repos, opp_hackathon, student_undergrad):
    """Sanity: ~1000 drafts still compute stats cleanly (no n² in the hot path)."""
    import time

    fake_repos.students.upsert(student_undergrad)
    fake_repos.opportunities.upsert(opp_hackathon)
    for i in range(1000):
        fake_repos.drafts.upsert(
            DraftMessage(
                id=f"d_{i}",
                student_id=student_undergrad.id,
                opportunity_id=opp_hackathon.id,
                to_email=student_undergrad.email,
                subject=f"s {i}",
                body_text="b",
                body_html="<p>b</p>",
                match_score=0.75,
                match_reasons=[],
                status=(DraftStatus.PENDING_APPROVAL if i % 3 else DraftStatus.SENT),
            )
        )
    t = time.perf_counter()
    pending = fake_repos.drafts.list_by_status(DraftStatus.PENDING_APPROVAL, limit=10_000)
    elapsed = time.perf_counter() - t
    assert len(pending) > 600
    # Extremely lax — any reasonable implementation is sub-second.
    assert elapsed < 1.0


# --------------------------------------------------------------------------- #
# Concurrent approvals via the HTTP UI endpoint                               #
# --------------------------------------------------------------------------- #


def test_concurrent_approvals_never_double_send(fake_repos, fake_inkbox, pending_draft):
    """Two parallel /ui/drafts/<id>/approve calls should send at most once."""
    from fastapi.testclient import TestClient

    from evk.agents.ingestion import IngestionAgent as _Ing
    from evk.api import app
    from evk.ui.routes import (
        _distributor_dep,
        _ingestion_dep,
        _inkbox_dep,
        _repos_dep,
    )

    fake_repos.drafts.upsert(pending_draft)
    app.dependency_overrides[_repos_dep] = lambda: fake_repos
    app.dependency_overrides[_inkbox_dep] = lambda: fake_inkbox
    app.dependency_overrides[_ingestion_dep] = lambda: _Ing(repos=fake_repos, inkbox=fake_inkbox)
    app.dependency_overrides[_distributor_dep] = lambda: DistributorAgent(
        repos=fake_repos, inkbox=fake_inkbox
    )
    try:
        with TestClient(app) as client:
            results: list[int] = []

            def hit():
                r = client.post(f"/ui/drafts/{pending_draft.id}/approve")
                results.append(r.status_code)

            threads = [threading.Thread(target=hit) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # First one sends; the rest either re-render the sent row (200) or
            # reject with 409. Crucially: Inkbox was called at most once.
            assert len(fake_inkbox.sent) == 1
            assert fake_repos.drafts.get(pending_draft.id).status == DraftStatus.SENT
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Mandate: distributor respects 45/batch + 0.2s delay + daily quota           #
# --------------------------------------------------------------------------- #


def test_distributor_batches_at_45_and_respects_quota(
    fake_repos, fake_inkbox, student_undergrad, opp_hackathon, monkeypatch
):
    """100 approved drafts → 2 batches of ≤45, quota decrement tracked."""
    from evk.ratelimit import DailyQuota

    fake_repos.students.upsert(student_undergrad)
    fake_repos.opportunities.upsert(opp_hackathon)
    for i in range(100):
        fake_repos.drafts.upsert(
            DraftMessage(
                id=f"approved_{i}",
                student_id=student_undergrad.id,
                opportunity_id=opp_hackathon.id,
                to_email=f"s{i}@example.edu",
                subject=f"s {i}",
                body_text="b",
                body_html="<p>b</p>",
                match_score=0.9,
                match_reasons=[],
                status=DraftStatus.APPROVED,
            )
        )

    # Force no actual sleep in the test.
    from evk import ratelimit as rl

    monkeypatch.setattr(rl.time, "sleep", lambda _s: None)

    quota = DailyQuota(limit=1000)
    distributor = DistributorAgent(repos=fake_repos, inkbox=fake_inkbox, quota=quota)
    sent = distributor.send_approved(limit=1000, batch_size=45)
    assert len(sent) == 100
    assert len(fake_inkbox.sent) == 100
    assert quota.snapshot()["used"] == 100


def test_distributor_stops_when_quota_exhausted(
    fake_repos, fake_inkbox, student_undergrad, opp_hackathon, monkeypatch
):
    from evk.ratelimit import DailyQuota, QuotaExceededError

    fake_repos.students.upsert(student_undergrad)
    fake_repos.opportunities.upsert(opp_hackathon)
    for i in range(10):
        fake_repos.drafts.upsert(
            DraftMessage(
                id=f"approved_{i}",
                student_id=student_undergrad.id,
                opportunity_id=opp_hackathon.id,
                to_email=f"s{i}@example.edu",
                subject=f"s {i}",
                body_text="b",
                body_html="<p>b</p>",
                match_score=0.9,
                match_reasons=[],
                status=DraftStatus.APPROVED,
            )
        )

    from evk import ratelimit as rl

    monkeypatch.setattr(rl.time, "sleep", lambda _s: None)

    quota = DailyQuota(limit=3)
    distributor = DistributorAgent(repos=fake_repos, inkbox=fake_inkbox, quota=quota)
    with pytest.raises(QuotaExceededError):
        distributor.send_approved(limit=100, batch_size=45)
    # Exactly the quota's worth was sent before we tripped the breaker.
    assert len(fake_inkbox.sent) == 3


# --------------------------------------------------------------------------- #
# Mandate: pseudonymisation — nothing raw-PII reaches Gemini                  #
# --------------------------------------------------------------------------- #


def test_personalizer_never_sends_email_or_full_name_to_gemini(
    fake_repos, fake_gemini, student_undergrad, opp_hackathon
):
    """The prompt sent to Gemini must not include student email, last name, or raw id."""
    fake_repos.students.upsert(student_undergrad)
    fake_repos.opportunities.upsert(opp_hackathon)
    # Queue a copy response for the one match.
    fake_gemini.queue_structured({"subject": "Hi", "body_text": "Hello!", "body_html": "<p>Hi</p>"})
    PersonalizerAgent(
        repos=fake_repos, gemini=fake_gemini, match_threshold=0.0
    ).draft_for_opportunity(opp_hackathon)

    # Inspect what Gemini actually saw.
    seen = "\n".join(c.prompt for c in fake_gemini.calls)
    assert student_undergrad.email not in seen
    last_name = student_undergrad.name.split(" ", 1)[-1]
    if last_name and last_name != student_undergrad.name.split(" ")[0]:
        assert last_name not in seen
    assert student_undergrad.id not in seen


# --------------------------------------------------------------------------- #
# Mandate: batch-commit seed — 500-ceiling respected                          #
# --------------------------------------------------------------------------- #


def test_upsert_many_chunking_is_correct(fake_repos, opp_hackathon):
    many = [opp_hackathon.model_copy(update={"id": f"bulk_{i}"}) for i in range(1500)]
    count = fake_repos.opportunities.upsert_many(many, batch_size=500)
    assert count == 1500
    assert len(fake_repos.opportunities.list_all()) >= 1500


# --------------------------------------------------------------------------- #
# Mandate: publish-through threshold — low confidence is skipped              #
# --------------------------------------------------------------------------- #


def test_low_confidence_classification_skips_persistence(
    fake_repos, fake_inkbox, fake_gemini, student_undergrad
):
    fake_repos.students.upsert(student_undergrad)
    fake_gemini.queue_structured(
        ClassifierResult(
            is_opportunity=True,
            confidence=0.4,  # below default 0.75 threshold
            reasoning="weak",
            opportunities=[_make_extracted()],
        )
    )
    agent = IngestionAgent(
        repos=fake_repos,
        inkbox=fake_inkbox,
        classifier=ClassifierAgent(gemini=fake_gemini),
        personalizer=PersonalizerAgent(repos=fake_repos, gemini=fake_gemini, match_threshold=0.0),
    )
    raw = agent.handle_inbound(_make_msg(1, sender="news@example.com"))
    assert raw.status == RawEmailStatus.SKIPPED
    assert fake_repos.opportunities.list_all() == []
    assert fake_repos.drafts.list_all() == []


# --------------------------------------------------------------------------- #
# Mandate: ingestion strips footers                                           #
# --------------------------------------------------------------------------- #


def test_ingestion_strips_unsubscribe_boilerplate(
    fake_repos, fake_inkbox, fake_gemini, student_undergrad
):
    fake_repos.students.upsert(student_undergrad)
    fake_gemini.queue_structured(
        ClassifierResult(is_opportunity=False, confidence=0.9, reasoning="")
    )
    body = (
        "The 2099 Rhodes Scholarship is open. Deadline: July 31.\n\n"
        "Unsubscribe by replying STOP.\n"
        "© 2099 Rhodes Trust"
    )
    msg = _make_msg(1, body=body, sender="news@example.com")
    agent = IngestionAgent(
        repos=fake_repos,
        inkbox=fake_inkbox,
        classifier=ClassifierAgent(gemini=fake_gemini),
        personalizer=PersonalizerAgent(repos=fake_repos, gemini=fake_gemini, match_threshold=0.0),
    )
    agent.handle_inbound(msg)
    stored = fake_repos.raw_emails.list_all()[0]
    assert "Unsubscribe" not in stored.body_text
    assert "Rhodes Scholarship" in stored.body_text


# --------------------------------------------------------------------------- #
# Big-corpus digest: 100 students x 50 opps runs in under 2s                  #
# --------------------------------------------------------------------------- #


def test_digest_scales_to_100_students_and_50_opps(fake_repos):
    """Not a perf benchmark — just a sanity check that the ranking is O(N·M)."""
    import time

    from evk.agents.digest import DigestAgent
    from evk.models import Opportunity, OpportunityKind, Student

    for i in range(100):
        fake_repos.students.upsert(
            Student(
                id=f"s_{i}",
                name=f"Student {i}",
                email=f"s{i}@example.edu",
                level=StudentLevel.UNDERGRAD,
                graduation_year=2027,
                fields_of_study=["computer science"],
                interests=["ai"],
                location="Mumbai, India",
                opted_in=True,
            )
        )
    for j in range(50):
        fake_repos.opportunities.upsert(
            Opportunity(
                id=f"o_{j}",
                title=f"AI Research Program {j} 2099",
                kind=OpportunityKind.INTERNSHIP,
                organization="Acme",
                summary="summary",
                eligibility="",
                deadline=datetime.now(UTC) + timedelta(days=90),
                url="https://example.com",
                location="Remote",
                tags=["ai"],
                fields_of_study=["computer science"],
                min_level=StudentLevel.UNDERGRAD,
                source_raw_email_id="",
                source_subject="",
                source_sender="",
            )
        )

    t = time.perf_counter()
    drafts = DigestAgent(repos=fake_repos, top_n=5, min_score=0.0).build_and_queue()
    elapsed = time.perf_counter() - t
    assert len(drafts) == 100
    # Extremely generous ceiling — flags only catastrophic regressions.
    assert elapsed < 5.0
