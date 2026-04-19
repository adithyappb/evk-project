"""FastAPI endpoint tests using TestClient + dependency overrides."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from evk.agents.classifier import ClassifierAgent
from evk.agents.ingestion import IngestionAgent
from evk.agents.personalizer import PersonalizerAgent
from evk.api import _distributor, _ingestion, _inkbox, _repos, app
from evk.inkbox_client import InboundMessage
from evk.models import (
    ClassifierResult,
    DraftStatus,
)

SIGNING_KEY = "whsec_test_signing_key"


def _sign(body: bytes, request_id: str, timestamp: str) -> str:
    message = f"{request_id}.{timestamp}.{body.decode()}"
    return "sha256=" + hmac.new(SIGNING_KEY.encode(), message.encode(), hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- #
# Client fixture with everything mocked.                                      #
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(fake_repos, fake_inkbox, fake_gemini) -> Iterator[TestClient]:
    classifier = ClassifierAgent(gemini=fake_gemini)
    personalizer = PersonalizerAgent(repos=fake_repos, gemini=fake_gemini, match_threshold=0.0)

    def _override_repos():
        return fake_repos

    def _override_inkbox():
        return fake_inkbox

    def _override_ingestion():
        return IngestionAgent(
            repos=fake_repos,
            inkbox=fake_inkbox,
            classifier=classifier,
            personalizer=personalizer,
        )

    def _override_distributor():
        from evk.agents.distributor import DistributorAgent

        return DistributorAgent(repos=fake_repos, inkbox=fake_inkbox)

    app.dependency_overrides[_repos] = _override_repos
    app.dependency_overrides[_inkbox] = _override_inkbox
    app.dependency_overrides[_ingestion] = _override_ingestion
    app.dependency_overrides[_distributor] = _override_distributor
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Health                                                                      #
# --------------------------------------------------------------------------- #


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# --------------------------------------------------------------------------- #
# Webhook                                                                     #
# --------------------------------------------------------------------------- #


def test_webhook_rejects_missing_signature(client):
    r = client.post("/webhooks/inkbox", content=b"{}")
    assert r.status_code == 401


def test_webhook_rejects_bad_signature(client):
    body = b'{"event":"message.received","data":{"id":"msg_x"}}'
    ts = str(int(time.time()))
    r = client.post(
        "/webhooks/inkbox",
        content=body,
        headers={
            "X-Inkbox-Request-ID": "req_1",
            "X-Inkbox-Timestamp": ts,
            "X-Inkbox-Signature": "sha256=" + "0" * 64,
        },
    )
    assert r.status_code == 401


def test_webhook_ignores_non_received_events(client, fake_inkbox):
    body = b'{"event":"message.sent","data":{"id":"m"}}'
    ts = str(int(time.time()))
    sig = _sign(body, "r1", ts)
    r = client.post(
        "/webhooks/inkbox",
        content=body,
        headers={
            "X-Inkbox-Request-ID": "r1",
            "X-Inkbox-Timestamp": ts,
            "X-Inkbox-Signature": sig,
        },
    )
    assert r.status_code == 202
    assert r.json()["ignored"] is True


def test_webhook_processes_received(client, fake_inkbox, fake_gemini):
    fake_inkbox.inbound_queue.append(
        InboundMessage(
            id="msg_abc",
            rfc_message_id=None,
            thread_id=None,
            from_address="x@x.com",
            subject="s",
            body_text="body",
            body_html="",
            raw=None,
        )
    )
    fake_gemini.queue_structured(
        ClassifierResult(is_opportunity=False, confidence=0.8, reasoning="no")
    )
    body = json.dumps({"event": "message.received", "data": {"id": "msg_abc"}}).encode()
    ts = str(int(time.time()))
    sig = _sign(body, "r1", ts)
    r = client.post(
        "/webhooks/inkbox",
        content=body,
        headers={
            "X-Inkbox-Request-ID": "r1",
            "X-Inkbox-Timestamp": ts,
            "X-Inkbox-Signature": sig,
        },
    )
    assert r.status_code == 202, r.text
    assert r.json()["ok"] is True


def test_webhook_404_for_unknown_message(client):
    body = json.dumps({"event": "message.received", "data": {"id": "missing"}}).encode()
    ts = str(int(time.time()))
    sig = _sign(body, "r1", ts)
    r = client.post(
        "/webhooks/inkbox",
        content=body,
        headers={
            "X-Inkbox-Request-ID": "r1",
            "X-Inkbox-Timestamp": ts,
            "X-Inkbox-Signature": sig,
        },
    )
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Admin: drafts                                                               #
# --------------------------------------------------------------------------- #


def test_list_drafts_filters_by_status(client, fake_repos, pending_draft):
    fake_repos.drafts.upsert(pending_draft)
    r = client.get("/admin/drafts?status_filter=pending_approval")
    assert r.status_code == 200, r.text
    assert len(r.json()) == 1

    r2 = client.get("/admin/drafts?status_filter=sent")
    assert r2.json() == []


def test_approve_without_send_updates_status(client, fake_repos, pending_draft):
    fake_repos.drafts.upsert(pending_draft)
    r = client.post(
        f"/admin/drafts/{pending_draft.id}/approve",
        json={"approver": "alice", "send_now": False},
    )
    assert r.status_code == 200, r.text
    updated = fake_repos.drafts.get(pending_draft.id)
    assert updated.status is DraftStatus.APPROVED


def test_approve_with_send_sends_immediately(client, fake_repos, fake_inkbox, pending_draft):
    fake_repos.drafts.upsert(pending_draft)
    r = client.post(
        f"/admin/drafts/{pending_draft.id}/approve",
        json={"approver": "alice", "send_now": True},
    )
    assert r.status_code == 200
    updated = fake_repos.drafts.get(pending_draft.id)
    assert updated.status is DraftStatus.SENT
    assert len(fake_inkbox.sent) == 1


def test_approve_missing_404(client):
    r = client.post("/admin/drafts/nope/approve", json={"send_now": False})
    assert r.status_code == 404


def test_reject_draft(client, fake_repos, pending_draft):
    fake_repos.drafts.upsert(pending_draft)
    r = client.post(
        f"/admin/drafts/{pending_draft.id}/reject",
        json={"approver": "alice"},
    )
    assert r.status_code == 200
    assert fake_repos.drafts.get(pending_draft.id).status is DraftStatus.REJECTED


def test_cannot_approve_already_sent(client, fake_repos, pending_draft):
    pending_draft.status = DraftStatus.SENT
    fake_repos.drafts.upsert(pending_draft)
    r = client.post(
        f"/admin/drafts/{pending_draft.id}/approve",
        json={"send_now": False},
    )
    assert r.status_code == 409


# --------------------------------------------------------------------------- #
# Admin: students / opportunities                                             #
# --------------------------------------------------------------------------- #


def test_upsert_and_list_students(client, fake_repos):
    payload = {
        "id": "s_new",
        "name": "New Student",
        "email": "new@example.com",
        "level": "undergrad",
        "fields_of_study": ["cs"],
        "interests": ["ai"],
    }
    r = client.post("/admin/students", json=payload)
    assert r.status_code == 200, r.text
    r2 = client.get("/admin/students")
    ids = {s["id"] for s in r2.json()}
    assert "s_new" in ids


def test_list_opportunities(client, fake_repos, opp_hackathon):
    fake_repos.opportunities.upsert(opp_hackathon)
    r = client.get("/admin/opportunities")
    assert r.status_code == 200
    assert len(r.json()) == 1


# --------------------------------------------------------------------------- #
# Admin: poll                                                                 #
# --------------------------------------------------------------------------- #


def test_admin_poll(client, fake_inkbox, fake_gemini):
    fake_inkbox.inbound_queue.append(
        InboundMessage(
            id="msg_poll",
            rfc_message_id=None,
            thread_id=None,
            from_address="x@x.com",
            subject="s",
            body_text="body",
            body_html="",
            raw=None,
        )
    )
    fake_gemini.queue_structured(
        ClassifierResult(is_opportunity=False, confidence=0.9, reasoning="no")
    )
    r = client.post("/admin/poll")
    assert r.status_code == 200
    data = r.json()
    assert data["processed"] == 1
    assert data["pending_drafts"] == 0
