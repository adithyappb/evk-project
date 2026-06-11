"""UI polish, accessibility, and template rendering checks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from evk.agents.digest import DigestAgent
from evk.api import app
from evk.auth import AuthService
from evk.config import get_settings
from evk.models import DraftStatus, Opportunity, OpportunityKind, Student, StudentLevel
from evk.ui.routes import _auth_dep, _distributor_dep, _ingestion_dep, _inkbox_dep, _repos_dep
from tests.test_ui import CaptureNotifier, _login


@pytest.fixture
def polish_client(fake_repos, fake_inkbox):
    settings = get_settings()
    notifier = CaptureNotifier()
    service = AuthService(repos=fake_repos, notifier=notifier, settings=settings)
    service.ensure_bootstrap()
    app.dependency_overrides[_repos_dep] = lambda: fake_repos
    app.dependency_overrides[_inkbox_dep] = lambda: fake_inkbox
    app.dependency_overrides[_ingestion_dep] = lambda: __import__(
        "evk.agents.ingestion", fromlist=["IngestionAgent"]
    ).IngestionAgent(repos=fake_repos, inkbox=fake_inkbox)
    app.dependency_overrides[_distributor_dep] = lambda: __import__(
        "evk.agents.distributor", fromlist=["DistributorAgent"]
    ).DistributorAgent(repos=fake_repos, inkbox=fake_inkbox)
    app.dependency_overrides[_auth_dep] = lambda: service
    with TestClient(app) as client:
        yield client, notifier, service
    app.dependency_overrides.clear()


def test_skip_link_and_main_landmark(polish_client):
    client, _, _ = polish_client
    body = client.get("/").text
    assert 'href="#main-content"' in body
    assert 'id="main-content"' in body
    assert "Skip to main content" in body


def test_student_dashboard_has_mobile_nav_and_a11y(polish_client, fake_repos, student_undergrad, pending_draft, opp_hackathon):
    client, notifier, service = polish_client
    fake_repos.students.upsert(student_undergrad)
    fake_repos.opportunities.upsert(opp_hackathon)
    pending_draft.status = DraftStatus.SENT
    fake_repos.drafts.upsert(pending_draft)
    service.ensure_bootstrap()
    _login(client, notifier, student_undergrad.email, get_settings().auth_local_demo_password)
    body = client.get("/app/student").text
    assert "Student view" in body
    assert "Recent outreach" in body
    assert "id=\"mobile-nav\"" in body
    assert "Opportunities picked for you" in body
    assert "human" in body.lower()


def test_opportunity_deadline_colors_use_static_tailwind(polish_client, fake_repos):
    """Dynamic Tailwind classes must not appear — they fail to compile on CDN."""
    client, notifier, _ = polish_client
    deadline = datetime.now(UTC) + timedelta(days=3)
    opp = Opportunity(
        id="opp_urgent_ui",
        title="Urgent Scholarship",
        kind=OpportunityKind.SCHOLARSHIP,
        organization="EVkids",
        summary="Apply soon",
        eligibility="",
        deadline=deadline,
        url="https://example.org",
        min_level=StudentLevel.GRADE_12,
    )
    fake_repos.opportunities.upsert(opp)
    _login(client, notifier, "admin@evkids.org", get_settings().auth_local_demo_password)
    body = client.get("/opportunities").text
    assert "text-rose-600" in body
    assert "text-{{" not in body


def test_digest_email_is_student_friendly(fake_repos):
    student = Student(
        id="s_digest_ui",
        name="Taylor Kim",
        email="taylor@example.edu",
        level=StudentLevel.GRADE_12,
        graduation_year=2027,
        fields_of_study=["technology"],
        interests=["ai"],
        location="Boston",
        opted_in=True,
    )
    opp = Opportunity(
        id="o_digest_ui",
        title="Tech Fellowship 2099",
        kind=OpportunityKind.FELLOWSHIP,
        organization="Acme",
        summary="Build cool things.",
        eligibility="",
        deadline=datetime.now(UTC) + timedelta(days=14),
        url="https://example.org/apply",
        location="Remote",
        tags=["tech"],
        fields_of_study=["technology"],
        min_level=StudentLevel.GRADE_12,
        source_raw_email_id="",
        source_subject="",
        source_sender="",
    )
    fake_repos.students.upsert(student)
    fake_repos.opportunities.upsert(opp)
    digest = DigestAgent(repos=fake_repos, top_n=3, min_score=0.0).build_one(student.id)
    assert digest is not None
    html = digest.body_html
    assert "Hi Taylor" in html
    assert "View &amp; apply" in html
    assert "real person" in html.lower() or "human-reviewed" in html.lower()
    assert "Tech Fellowship 2099" in html


def test_student_can_open_opportunity_detail(polish_client, fake_repos, student_undergrad, opp_hackathon):
    client, notifier, service = polish_client
    fake_repos.students.upsert(student_undergrad)
    fake_repos.opportunities.upsert(opp_hackathon)
    service.ensure_bootstrap()
    _login(client, notifier, student_undergrad.email, get_settings().auth_local_demo_password)
    body = client.get(f"/app/student/opportunities/{opp_hackathon.id}").text
    assert opp_hackathon.title in body
    assert "Open &amp; apply" in body
    assert "Track your progress" in body


def test_recommendations_use_match_scores(polish_client, fake_repos, student_undergrad, opp_hackathon):
    client, notifier, service = polish_client
    fake_repos.students.upsert(student_undergrad)
    fake_repos.opportunities.upsert(opp_hackathon)
    service.ensure_bootstrap()
    _login(client, notifier, student_undergrad.email, get_settings().auth_local_demo_password)
    body = client.get("/app/student").text
    assert "Matched for you" in body
    assert student_undergrad.name.split()[0] in body
    assert "Opportunities picked for you" in body
    assert opp_hackathon.title in body
    assert "% fit" in body


def test_recommend_for_student_helper(fake_repos, student_undergrad, opp_hackathon):
    from evk.ui.helpers import recommend_for_student
    from evk.ui.routes import _decorate_opps

    fake_repos.students.upsert(student_undergrad)
    fake_repos.opportunities.upsert(opp_hackathon)
    recs = recommend_for_student(student_undergrad, fake_repos, _decorate_opps, limit=5)
    assert len(recs) >= 1
    assert recs[0].match_score is not None
    assert recs[0].match_score >= 0.35
