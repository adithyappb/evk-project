"""HTML dashboard route tests. Renders real Jinja templates end-to-end."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from evk.agents.distributor import DistributorAgent
from evk.agents.ingestion import IngestionAgent
from evk.api import app
from evk.models import DraftStatus
from evk.ui.routes import (
    _distributor_dep,
    _ingestion_dep,
    _inkbox_dep,
    _repos_dep,
)


@pytest.fixture
def ui_client(fake_repos, fake_inkbox) -> Iterator[TestClient]:
    app.dependency_overrides[_repos_dep] = lambda: fake_repos
    app.dependency_overrides[_inkbox_dep] = lambda: fake_inkbox
    app.dependency_overrides[_ingestion_dep] = lambda: IngestionAgent(
        repos=fake_repos, inkbox=fake_inkbox
    )
    app.dependency_overrides[_distributor_dep] = lambda: DistributorAgent(
        repos=fake_repos, inkbox=fake_inkbox
    )
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_dashboard_renders(ui_client, fake_repos, opp_hackathon, student_undergrad):
    fake_repos.students.upsert(student_undergrad)
    fake_repos.opportunities.upsert(opp_hackathon)
    r = ui_client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "EVK" in body
    assert "Drafts" in body
    assert opp_hackathon.title in body
    assert student_undergrad.name in body


def test_stats_fragment(ui_client, fake_repos, pending_draft):
    fake_repos.drafts.upsert(pending_draft)
    r = ui_client.get("/ui/stats")
    assert r.status_code == 200
    assert "Pending approvals" in r.text


def test_drafts_fragment_renders_row(ui_client, fake_repos, pending_draft):
    fake_repos.drafts.upsert(pending_draft)
    r = ui_client.get("/ui/drafts?status_filter=pending_approval")
    assert r.status_code == 200
    # Jinja auto-escapes — check a substring that contains no special chars.
    assert "love Hack the North" in r.text
    assert "Approve" in r.text


def test_drafts_fragment_empty_state(ui_client):
    r = ui_client.get("/ui/drafts?status_filter=sent")
    assert r.status_code == 200
    assert "Nothing" in r.text


def test_ui_approve_sends_and_swaps(ui_client, fake_repos, fake_inkbox, pending_draft):
    fake_repos.drafts.upsert(pending_draft)
    r = ui_client.post(f"/ui/drafts/{pending_draft.id}/approve")
    assert r.status_code == 200
    updated = fake_repos.drafts.get(pending_draft.id)
    assert updated.status == DraftStatus.SENT
    assert len(fake_inkbox.sent) == 1
    assert "sent" in r.text.lower()


def test_ui_reject_swaps(ui_client, fake_repos, pending_draft):
    fake_repos.drafts.upsert(pending_draft)
    r = ui_client.post(f"/ui/drafts/{pending_draft.id}/reject")
    assert r.status_code == 200
    assert fake_repos.drafts.get(pending_draft.id).status == DraftStatus.REJECTED
    assert "rejected" in r.text.lower()


def test_ui_approve_404_when_missing(ui_client):
    r = ui_client.post("/ui/drafts/does-not-exist/approve")
    assert r.status_code == 404


def test_toast_is_dismissible_and_has_close_button(ui_client):
    """Every server-issued toast must ship a close button + a TTL the JS can read."""
    r = ui_client.post("/ui/remind")
    assert r.status_code == 200
    body = r.text
    # Toast is emitted as an out-of-band swap into #toasts
    assert 'hx-swap-oob="beforeend:#toasts"' in body
    assert "Reminder sweep" in body
    # Close control exists and is keyboard-accessible
    assert "data-toast-close" in body
    assert 'aria-label="Dismiss notification"' in body
    # TTL is advertised to the JS
    assert 'data-toast-ttl="4500"' in body
    # Progress bar is rendered
    assert "data-toast-progress" in body
    # Live region for accessibility
    assert 'aria-live="polite"' in body


def test_digest_toast_even_when_zero_queued(ui_client):
    """Digest returning 0 drafts still emits a toast — and that toast is dismissible."""
    r = ui_client.post("/ui/digest")
    assert r.status_code == 200
    assert "Weekly digest queued" in r.text
    assert "data-toast-close" in r.text


def test_stats_fragment_without_flash_has_no_toast(ui_client):
    """Plain GET /ui/stats must NOT emit a toast (avoids spurious popups on refresh)."""
    r = ui_client.get("/ui/stats")
    assert r.status_code == 200
    assert "hx-swap-oob" not in r.text
    assert "data-toast" not in r.text


def test_dashboard_loads_toast_script(ui_client):
    """The base template ships the hardened dismiss/arm/pause logic."""
    r = ui_client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "htmx:oobAfterSwap" in body
    assert "htmx:afterSwap" in body
    assert "data-toast-close" in body or "data-toast-close]" in body
    assert "Escape" in body


def test_stat_cards_are_clickable_anchors(ui_client):
    """Every stat card is a real <a> with a scroll target so it's keyboard-navigable."""
    r = ui_client.get("/ui/stats")
    assert r.status_code == 200
    body = r.text
    # 4 cards, 4 anchor targets
    assert body.count("data-stat-card") == 4
    assert 'href="#drafts"' in body          # pending + sent today
    assert 'href="#opportunities"' in body   # opportunities card
    assert 'href="#students"' in body        # students card
    # Pending + Sent-today also drive the draft tabs
    assert 'data-activate-tab="pending_approval"' in body
    assert 'data-activate-tab="sent"' in body
    # Every card is keyboard-accessible
    assert body.count("focus-visible:ring-") >= 4
    assert body.count("aria-label=") >= 4


def test_dashboard_sections_have_scroll_anchors(ui_client, fake_repos, opp_hackathon, student_undergrad):
    """The dashboard shell exposes #drafts / #students / #opportunities so stat-card links actually land."""
    fake_repos.students.upsert(student_undergrad)
    fake_repos.opportunities.upsert(opp_hackathon)
    r = ui_client.get("/")
    assert r.status_code == 200
    body = r.text
    assert 'id="drafts"' in body
    assert 'id="students"' in body
    assert 'id="opportunities"' in body


def test_draft_tabs_expose_data_attr_for_card_dispatch(ui_client):
    """The base dashboard emits data-draft-tab on every tab so the stat cards can drive them."""
    r = ui_client.get("/")
    assert r.status_code == 200
    body = r.text
    for value in ("pending_approval", "approved", "sent", "rejected", "failed"):
        assert f'data-draft-tab="{value}"' in body, value
    # Tab-state sync helper is wired up in the base template
    assert "setActiveTab" in body
    assert 'role="tablist"' in body


def test_dashboard_wires_stat_card_dispatcher(ui_client):
    """The JS dispatcher that handles card clicks ships in the base template."""
    r = ui_client.get("/")
    body = r.text
    assert "data-stat-card" in body
    assert "smoothScrollTo" in body
    assert "scrollIntoView" in body
