"""Test configuration: env defaults, cache resets, and shared fixtures."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

# --------------------------------------------------------------------------- #
# Ensure every Settings-required env var has a dummy value BEFORE import.     #
# --------------------------------------------------------------------------- #
os.environ.setdefault("EVK_MODE", "local")
os.environ.setdefault("INKBOX_API_KEY", "ApiKey_test")
os.environ.setdefault("INKBOX_AGENT_HANDLE", "evk-test")
os.environ.setdefault("INKBOX_SIGNING_KEY", "whsec_test_signing_key")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")
os.environ.setdefault("ADMIN_EMAIL", "admin@test.local")
os.environ.setdefault("APP_LOG_LEVEL", "WARNING")
os.environ.setdefault("REMINDER_DAYS_BEFORE", "7,2")

# Now imports that touch settings are safe.
from evk.config import get_settings
from evk.factory import reset_all_caches
from evk.models import (
    DraftMessage,
    DraftStatus,
    Opportunity,
    OpportunityKind,
    Student,
    StudentLevel,
)
from tests.fakes import (
    FakeGemini,
    FakeInkbox,
    build_fake_repos,
)


from fastapi.testclient import TestClient

from evk.ui.csrf import CSRF_COOKIE, CSRF_FORM_FIELD, CSRF_HEADER


@pytest.fixture(autouse=True)
def _csrf_client_post() -> None:
    """Inject CSRF token into TestClient form POSTs (matches browser double-submit cookie)."""
    original = TestClient.post

    def post(self, url, *args, data=None, **kwargs):
        token = self.cookies.get(CSRF_COOKIE)
        if not token:
            self.get("/")
            token = self.cookies.get(CSRF_COOKIE, "")
        if data is not None and isinstance(data, dict):
            enriched = dict(data)
            enriched.setdefault(CSRF_FORM_FIELD, token)
            data = enriched
        headers = dict(kwargs.get("headers") or {})
        if token:
            headers.setdefault(CSRF_HEADER, token)
        kwargs["headers"] = headers
        return original(self, url, *args, data=data, **kwargs)

    TestClient.post = post  # type: ignore[method-assign]
    yield
    TestClient.post = original  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> None:
    """Every test starts with a fresh Settings + factory cache."""
    get_settings.cache_clear()
    reset_all_caches()


@pytest.fixture
def fake_repos():
    return build_fake_repos()


@pytest.fixture
def fake_inkbox():
    return FakeInkbox()


@pytest.fixture
def fake_gemini():
    return FakeGemini()


# --------------------------------------------------------------------------- #
# Reusable domain fixtures                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
def student_undergrad() -> Student:
    return Student(
        id="student_ana",
        name="Ana Gomez",
        email="ana@example.edu",
        level=StudentLevel.UNDERGRAD,
        school="IIT Bombay",
        fields_of_study=["computer science", "artificial intelligence"],
        interests=["ai", "hackathons", "open-source"],
        location="Mumbai, India",
        bio="CS undergrad interested in AI.",
        opted_in=True,
    )


@pytest.fixture
def student_highschool() -> Student:
    return Student(
        id="student_max",
        name="Max Smith",
        email="max@example.edu",
        level=StudentLevel.GRADE_12,
        school="Lincoln HS",
        fields_of_study=["physics", "robotics"],
        interests=["robotics", "space", "competitions"],
        location="Austin, TX",
        bio="High-school robotics captain.",
        opted_in=True,
    )


@pytest.fixture
def opp_hackathon() -> Opportunity:
    return Opportunity(
        id="opp_hack",
        title="Hack the North 2026",
        kind=OpportunityKind.HACKATHON,
        organization="Uni of Waterloo",
        summary="36-hour hackathon for university students.",
        eligibility="University students 18+.",
        deadline=datetime(2026, 8, 15, 23, 59, 59, tzinfo=UTC),
        url="https://hackthenorth.com/",  # type: ignore[arg-type]
        location="Waterloo, Canada",
        tags=["hackathon", "coding"],
        fields_of_study=["computer science"],
        min_level=StudentLevel.UNDERGRAD,
    )


@pytest.fixture
def opp_highschool_sciencefair() -> Opportunity:
    return Opportunity(
        id="opp_isef",
        title="ISEF 2026",
        kind=OpportunityKind.COMPETITION,
        organization="Society for Science",
        summary="World's largest pre-college science fair.",
        eligibility="High school students.",
        deadline=datetime(2026, 5, 10, 23, 59, 59, tzinfo=UTC),
        url="https://www.societyforscience.org/isef/",  # type: ignore[arg-type]
        location="Columbus, OH",
        tags=["science-fair", "competitions", "research"],
        fields_of_study=["physics", "biology", "engineering"],
        min_level=StudentLevel.GRADE_12,
    )


@pytest.fixture
def pending_draft(student_undergrad, opp_hackathon) -> DraftMessage:
    return DraftMessage(
        id=f"{opp_hackathon.id}_{student_undergrad.id}",
        student_id=student_undergrad.id,
        opportunity_id=opp_hackathon.id,
        to_email=student_undergrad.email,
        subject="You'd love Hack the North",
        body_text="Hey Ana, ...",
        body_html="<p>Hey Ana, ...</p>",
        match_score=0.82,
        match_reasons=["field match", "interests match"],
        status=DraftStatus.PENDING_APPROVAL,
    )
