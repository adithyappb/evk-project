"""UI and auth route tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from evk.agents.distributor import DistributorAgent
from evk.agents.ingestion import IngestionAgent
from evk.api import app
from evk.auth import AuthNotifier, AuthService
from evk.config import get_settings
from evk.models import DraftStatus
from evk.ui.routes import (
    _auth_dep,
    _distributor_dep,
    _ingestion_dep,
    _inkbox_dep,
    _repos_dep,
)


class CaptureNotifier(AuthNotifier):
    def __init__(self) -> None:
        self.codes: dict[str, str] = {}

    def send_code(self, *, email: str, code: str) -> None:
        self.codes[email] = code


@pytest.fixture
def auth_service(fake_repos) -> tuple[AuthService, CaptureNotifier]:
    settings = get_settings()
    notifier = CaptureNotifier()
    service = AuthService(repos=fake_repos, notifier=notifier, settings=settings)
    service.ensure_bootstrap()
    return service, notifier


@pytest.fixture
def ui_client(fake_repos, fake_inkbox, auth_service) -> Iterator[TestClient]:
    service, _ = auth_service
    app.dependency_overrides[_repos_dep] = lambda: fake_repos
    app.dependency_overrides[_inkbox_dep] = lambda: fake_inkbox
    app.dependency_overrides[_ingestion_dep] = lambda: IngestionAgent(
        repos=fake_repos, inkbox=fake_inkbox
    )
    app.dependency_overrides[_distributor_dep] = lambda: DistributorAgent(
        repos=fake_repos, inkbox=fake_inkbox
    )
    app.dependency_overrides[_auth_dep] = lambda: service
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def _login(client: TestClient, notifier: CaptureNotifier, email: str, access_key: str) -> None:
    login = client.post("/auth/login", data={"email": email, "access_key": access_key})
    assert login.status_code == 200
    code = notifier.codes[email]
    verify = client.post(
        "/auth/verify",
        data={"email": email, "code": code},
        follow_redirects=False,
    )
    assert verify.status_code == 303


def test_front_page_has_centered_login_path(ui_client):
    response = ui_client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "Beautifully simple access to the EVkids opportunity ecosystem." in body
    assert "Existing user" in body
    assert "New user" in body
    assert 'http://testserver/login' in body
    assert 'http://testserver/register' in body


def test_login_page_defaults_to_existing_user(ui_client):
    response = ui_client.get("/login")
    assert response.status_code == 200
    body = response.text
    assert "Existing user" in body
    assert "Enter your details to continue." in body
    assert "Create your account." not in body
    assert "Full name" not in body
    assert "Local developer accounts" not in body


def test_register_page_renders_new_user_only(ui_client):
    response = ui_client.get("/register")
    assert response.status_code == 200
    body = response.text
    assert "New student account" in body
    assert "Create your account to track opportunities" in body
    assert "Full name" in body
    assert "Enter your details to continue." not in body
    assert "Existing user" not in body


def test_signup_creates_user_and_shows_verify(ui_client, fake_repos, auth_service):
    _, notifier = auth_service
    response = ui_client.post(
        "/auth/signup",
        data={
            "name": "Jordan Rivera",
            "email": "jordan@example.org",
            "role": "student",
            "organization": "City Youth Lab",
            "access_key": "StrongerPass123",
        },
    )
    assert response.status_code == 200
    assert "verification code" in response.text.lower()
    assert fake_repos.users.get_by_email("jordan@example.org") is not None
    user = fake_repos.users.get_by_email("jordan@example.org")
    assert user is not None
    assert user.role.value == "student"
    assert "jordan@example.org" in notifier.codes


def test_signup_rejects_staff_roles(ui_client, auth_service):
    response = ui_client.post(
        "/auth/signup",
        data={
            "name": "Bad Actor",
            "email": "bad@example.org",
            "role": "admin",
            "organization": "X",
            "access_key": "StrongerPass123",
        },
    )
    assert response.status_code == 400
    assert "students only" in response.text.lower()


def test_admin_login_reaches_admin_dashboard(ui_client, auth_service):
    _, notifier = auth_service
    _login(ui_client, notifier, "admin@evkids.org", get_settings().auth_local_demo_password)
    response = ui_client.get("/app/admin")
    assert response.status_code == 200
    body = response.text
    assert "Platform admin" in body
    assert "User management" in body
    assert "Open review queue" in body


def test_student_login_reaches_student_dashboard(ui_client, fake_repos, auth_service, student_undergrad, pending_draft, opp_hackathon):
    fake_repos.students.upsert(student_undergrad)
    fake_repos.opportunities.upsert(opp_hackathon)
    # Pending drafts must stay hidden from students until an admin approves them;
    # only SENT / APPROVED drafts appear under "Recent outreach".
    fake_repos.drafts.upsert(pending_draft)
    sent_draft = pending_draft.model_copy(
        update={
            "id": f"{pending_draft.id}_sent",
            "subject": "Your Hack the North invite is live",
            "status": DraftStatus.SENT,
        }
    )
    fake_repos.drafts.upsert(sent_draft)
    service, notifier = auth_service
    service.ensure_bootstrap()
    _login(ui_client, notifier, student_undergrad.email, get_settings().auth_local_demo_password)
    response = ui_client.get("/app/student")
    assert response.status_code == 200
    body = response.text
    assert "Student view" in body
    assert student_undergrad.name in body
    assert "Recent outreach" in body
    assert "Your Hack the North invite is live" in body  # SENT copy is visible
    assert "love Hack the North" not in body  # pending copy stays hidden


def test_ngo_login_reaches_partner_dashboard(ui_client, auth_service):
    _, notifier = auth_service
    _login(ui_client, notifier, "partner@evkids.org", get_settings().auth_local_demo_password)
    response = ui_client.get("/app/ngo")
    assert response.status_code == 200
    body = response.text
    assert "NGO admin" in body
    assert "Partner operations" in body
    assert "Tracked opportunities" in body


def test_admin_detail_pages_require_auth(ui_client):
    response = ui_client.get("/ui/pages/drafts/pending_approval", follow_redirects=False)
    assert response.status_code == 303


def test_admin_can_view_detail_pages_after_login(ui_client, fake_repos, auth_service, pending_draft):
    fake_repos.drafts.upsert(pending_draft)
    _, notifier = auth_service
    _login(ui_client, notifier, "admin@evkids.org", get_settings().auth_local_demo_password)
    response = ui_client.get("/ui/pages/drafts/pending_approval")
    assert response.status_code == 200
    assert "Back to dashboard" in response.text
    assert "Approve &amp; send" in response.text


def test_admin_can_toggle_user_active(ui_client, fake_repos, auth_service, student_undergrad):
    fake_repos.students.upsert(student_undergrad)
    service, notifier = auth_service
    service.ensure_bootstrap()
    target = fake_repos.users.get_by_email(student_undergrad.email)
    assert target is not None
    _login(ui_client, notifier, "admin@evkids.org", get_settings().auth_local_demo_password)
    response = ui_client.post(f"/ui/users/{target.id}/toggle-active")
    assert response.status_code == 200
    refreshed = fake_repos.users.get(target.id)
    assert refreshed is not None
    assert refreshed.is_active is False
    assert "inactive" in response.text.lower()


def test_ui_approve_sends_and_swaps(ui_client, fake_repos, fake_inkbox, auth_service, pending_draft):
    fake_repos.drafts.upsert(pending_draft)
    _, notifier = auth_service
    _login(ui_client, notifier, "admin@evkids.org", get_settings().auth_local_demo_password)
    response = ui_client.post(f"/ui/drafts/{pending_draft.id}/approve")
    assert response.status_code == 200
    updated = fake_repos.drafts.get(pending_draft.id)
    assert updated is not None
    assert updated.status == DraftStatus.SENT
    assert len(fake_inkbox.sent) == 1
    assert 'hx-swap-oob="outerHTML:#stats"' in response.text
    assert "Sent email to" in response.text


def test_toast_script_and_logout_present_for_signed_in_user(ui_client, auth_service):
    _, notifier = auth_service
    _login(ui_client, notifier, "admin@evkids.org", get_settings().auth_local_demo_password)
    response = ui_client.get("/app/admin")
    assert response.status_code == 200
    body = response.text
    assert "htmx:oobAfterSwap" in body
    assert "data-toast-close" in body or "data-toast-close]" in body
    assert "Sign out" in body
    assert "copyText" in body
