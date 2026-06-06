"""LocalStore: JSON-backed repos survive round-trip and reloads."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evk.local_store import build_local_repos
from evk.models import (
    DraftMessage,
    DraftStatus,
    Opportunity,
    OpportunityKind,
    Student,
    StudentLevel,
)


def _mk_student(i: int) -> Student:
    return Student(
        id=f"s{i}",
        name=f"Student {i}",
        email=f"s{i}@example.edu",
        level=StudentLevel.UNDERGRAD,
        fields_of_study=["cs"],
        interests=["ai"],
        opted_in=True,
    )


def test_local_repos_persist_across_reload(tmp_path):
    repos_a = build_local_repos(data_dir=tmp_path)
    for i in range(5):
        repos_a.students.upsert(_mk_student(i))

    # Fresh instance re-reads from disk.
    repos_b = build_local_repos(data_dir=tmp_path)
    students = repos_b.students.list_all()
    assert {s.id for s in students} == {f"s{i}" for i in range(5)}


def test_local_opportunity_deadline_filter(tmp_path):
    repos = build_local_repos(data_dir=tmp_path)
    now = datetime.now(UTC)
    soon = Opportunity(
        id="soon",
        title="Soon",
        kind=OpportunityKind.SCHOLARSHIP,
        organization="X",
        summary="",
        eligibility="",
        deadline=now + timedelta(days=3),
        min_level=StudentLevel.UNDERGRAD,
    )
    far = Opportunity(
        id="far",
        title="Far",
        kind=OpportunityKind.SCHOLARSHIP,
        organization="X",
        summary="",
        eligibility="",
        deadline=now + timedelta(days=60),
        min_level=StudentLevel.UNDERGRAD,
    )
    repos.opportunities.upsert(soon)
    repos.opportunities.upsert(far)
    found = repos.opportunities.with_upcoming_deadlines(within_days=7)
    assert [o.id for o in found] == ["soon"]


def test_local_draft_filter_by_status(tmp_path):
    repos = build_local_repos(data_dir=tmp_path)
    for i, status in enumerate(
        [DraftStatus.PENDING_APPROVAL, DraftStatus.SENT, DraftStatus.PENDING_APPROVAL]
    ):
        repos.drafts.upsert(
            DraftMessage(
                id=f"d{i}",
                student_id="s1",
                opportunity_id=f"o{i}",
                to_email="s1@example.edu",
                subject="s",
                body_text="b",
                body_html="<p>b</p>",
                match_score=0.6,
                match_reasons=["ok"],
                status=status,
            )
        )
    pending = repos.drafts.list_by_status(DraftStatus.PENDING_APPROVAL)
    assert len(pending) == 2
    sent = repos.drafts.list_by_status(DraftStatus.SENT)
    assert len(sent) == 1


def test_local_reminder_dedup(tmp_path):
    from evk.models import ReminderLog

    repos = build_local_repos(data_dir=tmp_path)
    repos.reminders.upsert(
        ReminderLog(id="r1", student_id="s1", opportunity_id="o1", days_before=7)
    )
    assert repos.reminders.exists("s1", "o1", 7) is True
    assert repos.reminders.exists("s1", "o1", 2) is False
