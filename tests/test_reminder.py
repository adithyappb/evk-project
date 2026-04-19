"""Reminder agent: window math + idempotency."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evk.agents.reminder import ReminderAgent, _closest_window
from evk.models import DraftMessage, DraftStatus


def test_closest_window():
    windows = [7, 2]
    assert _closest_window(6.5, windows) == 7
    assert _closest_window(6.0, windows) == 7
    assert _closest_window(1.5, windows) == 2
    assert _closest_window(1.9, windows) == 2
    # not in any window
    assert _closest_window(4.0, windows) is None
    assert _closest_window(10.0, windows) is None
    # exact window boundary
    assert _closest_window(7.0, windows) == 7


def _make_sent_draft(student, opp) -> DraftMessage:
    return DraftMessage(
        id=f"{opp.id}_{student.id}",
        student_id=student.id,
        opportunity_id=opp.id,
        to_email=student.email,
        subject="subj",
        body_text="body",
        match_score=0.7,
        status=DraftStatus.SENT,
    )


def test_reminder_sends_once_per_window(fake_repos, fake_inkbox, student_undergrad, opp_hackathon):
    # Set a deadline 2 days away so it falls into the 2-day window.
    opp_hackathon.deadline = datetime.now(UTC) + timedelta(days=1, hours=23)
    fake_repos.opportunities.upsert(opp_hackathon)
    fake_repos.students.upsert(student_undergrad)
    fake_repos.drafts.upsert(_make_sent_draft(student_undergrad, opp_hackathon))

    agent = ReminderAgent(repos=fake_repos, inkbox=fake_inkbox)
    n = agent.run()
    assert n == 1
    assert len(fake_inkbox.sent) == 1
    assert "2 day" in fake_inkbox.sent[0].subject.lower()

    # Run again — should be idempotent (already logged).
    n2 = agent.run()
    assert n2 == 0
    assert len(fake_inkbox.sent) == 1


def test_reminder_skips_opted_out(fake_repos, fake_inkbox, student_undergrad, opp_hackathon):
    student_undergrad.opted_in = False
    opp_hackathon.deadline = datetime.now(UTC) + timedelta(days=1, hours=23)
    fake_repos.opportunities.upsert(opp_hackathon)
    fake_repos.students.upsert(student_undergrad)
    fake_repos.drafts.upsert(_make_sent_draft(student_undergrad, opp_hackathon))

    n = ReminderAgent(repos=fake_repos, inkbox=fake_inkbox).run()
    assert n == 0
    assert fake_inkbox.sent == []


def test_reminder_skips_when_outside_any_window(
    fake_repos, fake_inkbox, student_undergrad, opp_hackathon
):
    # 4 days away — not in [7] or [2] windows
    opp_hackathon.deadline = datetime.now(UTC) + timedelta(days=4)
    fake_repos.opportunities.upsert(opp_hackathon)
    fake_repos.students.upsert(student_undergrad)
    fake_repos.drafts.upsert(_make_sent_draft(student_undergrad, opp_hackathon))

    n = ReminderAgent(repos=fake_repos, inkbox=fake_inkbox).run()
    assert n == 0


def test_reminder_skips_past_deadline(fake_repos, fake_inkbox, student_undergrad, opp_hackathon):
    opp_hackathon.deadline = datetime.now(UTC) - timedelta(days=1)
    fake_repos.opportunities.upsert(opp_hackathon)
    fake_repos.students.upsert(student_undergrad)
    fake_repos.drafts.upsert(_make_sent_draft(student_undergrad, opp_hackathon))

    n = ReminderAgent(repos=fake_repos, inkbox=fake_inkbox).run()
    assert n == 0
