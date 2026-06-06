"""Distributor agent tests."""

from __future__ import annotations

import pytest

from evk.agents.distributor import DistributorAgent
from evk.models import DraftStatus


def test_send_approved_sends_only_approved(fake_repos, fake_inkbox, pending_draft):
    fake_repos.drafts.upsert(pending_draft)  # status = PENDING
    dist = DistributorAgent(repos=fake_repos, inkbox=fake_inkbox)
    sent = dist.send_approved()
    assert sent == []
    assert fake_inkbox.sent == []


def test_approved_draft_gets_sent_and_updated(fake_repos, fake_inkbox, pending_draft):
    pending_draft.status = DraftStatus.APPROVED
    fake_repos.drafts.upsert(pending_draft)
    dist = DistributorAgent(repos=fake_repos, inkbox=fake_inkbox)

    sent = dist.send_approved()
    assert len(sent) == 1
    assert len(fake_inkbox.sent) == 1
    assert fake_inkbox.sent[0].subject == pending_draft.subject
    assert fake_inkbox.sent[0].to == [pending_draft.to_email]
    # status updated
    updated = fake_repos.drafts.get(pending_draft.id)
    assert updated.status is DraftStatus.SENT
    assert updated.sent_at is not None
    assert updated.inkbox_message_id is not None


def test_send_one_refuses_unapproved(fake_repos, fake_inkbox, pending_draft):
    dist = DistributorAgent(repos=fake_repos, inkbox=fake_inkbox)
    with pytest.raises(ValueError, match="not approved"):
        dist.send_one(pending_draft)


def test_send_failure_marks_draft_failed(fake_repos, fake_inkbox, pending_draft):
    pending_draft.status = DraftStatus.APPROVED
    fake_repos.drafts.upsert(pending_draft)

    def boom(**_kwargs):
        raise RuntimeError("simulated outage")

    fake_inkbox.send = boom  # type: ignore[method-assign]
    dist = DistributorAgent(repos=fake_repos, inkbox=fake_inkbox)
    sent = dist.send_approved()
    assert sent == []
    updated = fake_repos.drafts.get(pending_draft.id)
    assert updated.status is DraftStatus.FAILED
    assert updated.send_error is not None
    assert "simulated outage" in updated.send_error
