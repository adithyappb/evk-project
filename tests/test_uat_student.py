"""
UAT: Student role — full end-to-end acceptance tests.

Covers the complete journey of an EVkids student from first activation
through opportunity discovery, outcome tracking, and profile management.
Each test is intentionally written at the user-story level, not the unit level.

Run with:
    pytest tests/test_uat_student.py -v
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from evk.agents.distributor import DistributorAgent
from evk.agents.ingestion import IngestionAgent
from evk.api import app
from evk.auth import AuthNotifier, AuthService
from evk.config import get_settings
from evk.models import (
    AppUser,
    DraftMessage,
    DraftStatus,
    Opportunity,
    OpportunityKind,
    Student,
    StudentLevel,
    UserRole,
)
from evk.ui.routes import (
    _auth_dep,
    _distributor_dep,
    _ingestion_dep,
    _inkbox_dep,
    _repos_dep,
)
from tests.fakes import FakeStudentRepo, build_fake_repos


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class CaptureNotifier(AuthNotifier):
    def __init__(self) -> None:
        self.codes: dict[str, str] = {}

    def send_code(self, *, email: str, code: str) -> None:
        self.codes[email] = code


@pytest.fixture
def auth_service(fake_repos):
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
    assert login.status_code == 200, f"Login POST failed: {login.status_code}"
    code = notifier.codes[email]
    verify = client.post(
        "/auth/verify",
        data={"email": email, "code": code},
        follow_redirects=False,
    )
    assert verify.status_code == 303, f"OTP verify redirect expected, got {verify.status_code}"


def _make_student(student_id: str = "student_test", *, level: StudentLevel = StudentLevel.UNDERGRAD) -> Student:
    return Student(
        id=student_id,
        name="Priya Patel",
        email="priya@example.edu",
        level=level,
        school_name="MIT",
        fields_of_study=["computer science", "data science"],
        career_interests=["ai", "machine learning"],
        opportunity_types_sought=["internship", "hackathon"],
        location="Boston, MA",
        bio="CS undergrad passionate about AI.",
        opted_in=True,
    )


def _make_opp(opp_id: str = "opp_test", *, needs_review: bool = False) -> Opportunity:
    return Opportunity(
        id=opp_id,
        title="Google Summer of Code 2026",
        kind=OpportunityKind.INTERNSHIP,
        organization="Google",
        summary="12-week paid open-source internship.",
        eligibility="University students.",
        deadline=datetime(2026, 9, 1, 23, 59, tzinfo=UTC),
        url="https://summerofcode.withgoogle.com/",  # type: ignore[arg-type]
        location="Remote",
        tags=["open-source", "ai", "coding"],
        fields_of_study=["computer science"],
        min_level=StudentLevel.UNDERGRAD,
        needs_review=needs_review,
    )


def _make_draft(student: Student, opp: Opportunity, status: DraftStatus = DraftStatus.SENT) -> DraftMessage:
    return DraftMessage(
        id=f"{opp.id}_{student.id}",
        student_id=student.id,
        opportunity_id=opp.id,
        to_email=student.email,
        subject=f"You'd be great for: {opp.title}",
        body_text=f"Hi {student.name.split()[0]}, we thought this would fit you well.",
        body_html=f"<p>Hi {student.name.split()[0]},</p>",
        match_score=0.85,
        match_reasons=["field match: computer science", "interests match: ai"],
        status=status,
        sent_at=datetime.now(UTC) if status == DraftStatus.SENT else None,
    )


def _register_student_user(fake_repos, auth_service, student: Student) -> AppUser:
    """Ensure an AppUser exists for the student and return it.

    bootstrap_local_users() auto-creates a user for every student already in
    fake_repos.students. Call this AFTER the student is upserted so the second
    ensure_bootstrap() call picks it up.
    """
    service, _ = auth_service
    # Re-run bootstrap so it picks up the newly-added student record.
    service.ensure_bootstrap()
    user = fake_repos.users.get_by_email(student.email)
    assert user is not None, f"Bootstrap did not create user for {student.email}"
    return user


# ===========================================================================
# UAT-STU-01  First-time Activation via Welcome Email Link
# ===========================================================================


class TestStudentActivation:
    def test_valid_activation_token_shows_setup_form(self, ui_client, fake_repos):
        """Student receives a welcome email with a valid token and sees the setup form."""
        from datetime import timedelta
        import secrets
        token = secrets.token_hex(16)
        expires = datetime.now(UTC) + timedelta(days=7)
        # Create an inactive student user with an activation token
        from evk.auth import hash_access_key
        salt = secrets.token_hex(8)
        user = AppUser(
            id="user_new_student",
            email="newstudent@example.edu",
            name="New Student",
            role=UserRole.STUDENT,
            student_id="student_new",
            is_active=False,
            access_key_salt=salt,
            access_key_hash=hash_access_key("placeholder", salt=salt),
            activation_token=token,
            activation_token_expires=expires,
        )
        fake_repos.users.upsert(user)
        resp = ui_client.get(f"/profile/setup?token={token}&email=newstudent@example.edu")
        assert resp.status_code == 200
        assert "Set your password" in resp.text or "password" in resp.text.lower()

    def test_expired_token_shows_error_not_form(self, ui_client, fake_repos):
        """Student using an expired token is blocked and sees a clear error message."""
        from datetime import timedelta
        import secrets
        from evk.auth import hash_access_key
        token = secrets.token_hex(16)
        salt = secrets.token_hex(8)
        user = AppUser(
            id="user_expired",
            email="expired@example.edu",
            name="Expired User",
            role=UserRole.STUDENT,
            student_id=None,
            is_active=False,
            access_key_salt=salt,
            access_key_hash=hash_access_key("placeholder", salt=salt),
            activation_token=token,
            activation_token_expires=datetime.now(UTC) - timedelta(days=1),
        )
        fake_repos.users.upsert(user)
        resp = ui_client.get(f"/profile/setup?token={token}&email=expired@example.edu")
        assert resp.status_code == 200
        assert "expired" in resp.text.lower()

    def test_invalid_token_shows_error(self, ui_client):
        """Random / tampered tokens show an invalid-link error."""
        resp = ui_client.get("/profile/setup?token=badtoken&email=nobody@example.edu")
        assert resp.status_code == 200
        assert "invalid" in resp.text.lower() or "expired" in resp.text.lower()

    def test_setup_form_accepts_valid_password_and_redirects(self, ui_client, fake_repos):
        """Student completes setup: sets password + interests, is redirected to login."""
        from datetime import timedelta
        import secrets
        from evk.auth import hash_access_key
        token = secrets.token_hex(16)
        salt = secrets.token_hex(8)
        student = _make_student()
        fake_repos.students.upsert(student)
        user = AppUser(
            id="user_setup",
            email=student.email,
            name=student.name,
            role=UserRole.STUDENT,
            student_id=student.id,
            is_active=False,
            access_key_salt=salt,
            access_key_hash=hash_access_key("placeholder", salt=salt),
            activation_token=token,
            activation_token_expires=datetime.now(UTC) + timedelta(days=7),
        )
        fake_repos.users.upsert(user)
        resp = ui_client.post(
            "/profile/setup",
            data={
                "email": student.email,
                "token": token,
                "password": "SecurePass99!",
                "confirm_password": "SecurePass99!",
                "career_interests": ["ai", "machine learning"],
                "opportunity_types": ["internship"],
                "notification_frequency": "weekly",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        updated_user = fake_repos.users.get(user.id)
        assert updated_user is not None
        assert updated_user.activation_token is None  # token consumed

    def test_setup_rejects_password_mismatch(self, ui_client, fake_repos):
        """Mismatched passwords on setup form show an error and do not activate the account."""
        from datetime import timedelta
        import secrets
        from evk.auth import hash_access_key
        token = secrets.token_hex(16)
        salt = secrets.token_hex(8)
        user = AppUser(
            id="user_mismatch",
            email="mismatch@example.edu",
            name="Mismatch User",
            role=UserRole.STUDENT,
            student_id=None,
            is_active=False,
            access_key_salt=salt,
            access_key_hash=hash_access_key("placeholder", salt=salt),
            activation_token=token,
            activation_token_expires=datetime.now(UTC) + timedelta(days=7),
        )
        fake_repos.users.upsert(user)
        resp = ui_client.post(
            "/profile/setup",
            data={
                "email": "mismatch@example.edu",
                "token": token,
                "password": "SecurePass99!",
                "confirm_password": "DifferentPass1!",
                "notification_frequency": "weekly",
            },
        )
        assert resp.status_code == 200
        assert "match" in resp.text.lower()


# ===========================================================================
# UAT-STU-02  Login & MFA Flow
# ===========================================================================


class TestStudentAuthFlow:
    def test_login_page_renders(self, ui_client):
        """Landing and login pages are accessible without authentication."""
        assert ui_client.get("/").status_code == 200
        assert ui_client.get("/login").status_code == 200

    def test_full_login_mfa_flow_redirects_to_student_dashboard(
        self, ui_client, fake_repos, auth_service
    ):
        """Student logs in with correct password + OTP and lands on the student dashboard."""
        student = _make_student()
        fake_repos.students.upsert(student)
        service, notifier = auth_service
        _register_student_user(fake_repos, auth_service, student)
        _login(ui_client, notifier, student.email, get_settings().auth_local_demo_password)
        resp = ui_client.get("/app/student", follow_redirects=True)
        assert resp.status_code == 200
        assert "Student view" in resp.text

    def test_wrong_password_returns_error_not_otp(self, ui_client, fake_repos, auth_service):
        """Wrong password at the first factor shows an error without issuing an OTP."""
        student = _make_student()
        fake_repos.students.upsert(student)
        _register_student_user(fake_repos, auth_service, student)
        _, notifier = auth_service
        resp = ui_client.post(
            "/auth/login",
            data={"email": student.email, "access_key": "wrong-password"},
        )
        # Route returns 400 with error HTML for auth failures
        assert resp.status_code == 400
        assert student.email not in notifier.codes  # no OTP issued

    def test_wrong_otp_returns_error(self, ui_client, fake_repos, auth_service):
        """Incorrect OTP returns a 400 error page, not a session redirect."""
        student = _make_student()
        fake_repos.students.upsert(student)
        _register_student_user(fake_repos, auth_service, student)
        _, notifier = auth_service
        ui_client.post("/auth/login", data={"email": student.email, "access_key": get_settings().auth_local_demo_password})
        resp = ui_client.post(
            "/auth/verify",
            data={"email": student.email, "code": "000000"},
            follow_redirects=False,
        )
        # Route returns 400 with error HTML for failed OTP verification
        assert resp.status_code == 400
        assert "code" in resp.text.lower() or "verification" in resp.text.lower()

    def test_logout_clears_session_and_redirects_to_landing(self, ui_client, fake_repos, auth_service):
        """After logout, protected pages redirect to login."""
        student = _make_student()
        fake_repos.students.upsert(student)
        _register_student_user(fake_repos, auth_service, student)
        _, notifier = auth_service
        _login(ui_client, notifier, student.email, get_settings().auth_local_demo_password)
        logout = ui_client.post("/auth/logout", follow_redirects=False)
        assert logout.status_code == 303
        # After logout, /app/student should redirect to login
        resp = ui_client.get("/app/student", follow_redirects=False)
        assert resp.status_code == 303

    def test_app_redirects_unauthenticated_to_landing(self, ui_client):
        """Unauthenticated request to /app redirects to login."""
        resp = ui_client.get("/app", follow_redirects=False)
        assert resp.status_code == 303


# ===========================================================================
# UAT-STU-03  Student Dashboard — Opportunity Discovery
# ===========================================================================


class TestStudentDashboard:
    def _setup_and_login(self, ui_client, fake_repos, auth_service, student, opp=None, draft=None):
        fake_repos.students.upsert(student)
        if opp:
            fake_repos.opportunities.upsert(opp)
        if draft:
            fake_repos.drafts.upsert(draft)
        _register_student_user(fake_repos, auth_service, student)
        _, notifier = auth_service
        _login(ui_client, notifier, student.email, get_settings().auth_local_demo_password)

    def test_student_dashboard_loads_with_name(self, ui_client, fake_repos, auth_service):
        """Student dashboard renders and shows the student's name."""
        student = _make_student()
        self._setup_and_login(ui_client, fake_repos, auth_service, student)
        resp = ui_client.get("/app/student")
        assert resp.status_code == 200
        assert student.name in resp.text

    def test_student_dashboard_shows_sent_draft(self, ui_client, fake_repos, auth_service):
        """Student sees opportunities that were sent to them (approved + sent drafts)."""
        student = _make_student()
        opp = _make_opp()
        draft = _make_draft(student, opp, status=DraftStatus.SENT)
        self._setup_and_login(ui_client, fake_repos, auth_service, student, opp, draft)
        resp = ui_client.get("/app/student")
        assert resp.status_code == 200
        assert opp.title in resp.text

    def test_student_dashboard_does_not_show_pending_draft_content(self, ui_client, fake_repos, auth_service):
        """Pending-approval drafts are hidden from the student dashboard entirely.

        student_dashboard() filters to status IN (sent, approved) before passing
        drafts to the template, so students never see personalized copy before
        the human-approval gate has been cleared.
        """
        student = _make_student()
        opp = _make_opp()
        draft = _make_draft(student, opp, status=DraftStatus.PENDING_APPROVAL)
        self._setup_and_login(ui_client, fake_repos, auth_service, student, opp, draft)
        resp = ui_client.get("/app/student")
        assert resp.status_code == 200
        assert draft.subject not in resp.text
        assert draft.body_text not in resp.text

    def test_student_dashboard_shows_match_reasons(self, ui_client, fake_repos, auth_service):
        """Match reasons are surfaced to the student so they understand why they received this."""
        student = _make_student()
        opp = _make_opp()
        draft = _make_draft(student, opp, status=DraftStatus.SENT)
        self._setup_and_login(ui_client, fake_repos, auth_service, student, opp, draft)
        resp = ui_client.get("/app/student")
        assert resp.status_code == 200
        # Match reasons should be visible in the dashboard
        assert "field match" in resp.text or "computer science" in resp.text or "ai" in resp.text

    def test_student_sees_only_their_own_drafts(self, ui_client, fake_repos, auth_service):
        """A student cannot see drafts addressed to other students."""
        my_student = _make_student("student_me")
        other_student = Student(
            id="student_other",
            name="Other Person",
            email="other@example.edu",
            level=StudentLevel.UNDERGRAD,
            opted_in=True,
        )
        opp = _make_opp()
        my_draft = _make_draft(my_student, opp, status=DraftStatus.SENT)
        other_draft = DraftMessage(
            id=f"{opp.id}_{other_student.id}",
            student_id=other_student.id,
            opportunity_id=opp.id,
            to_email=other_student.email,
            subject="Only for Other Person",
            body_text="Private message for Other Person.",
            body_html="",
            match_score=0.9,
            match_reasons=[],
            status=DraftStatus.SENT,
        )
        fake_repos.students.upsert(my_student)
        fake_repos.students.upsert(other_student)
        fake_repos.opportunities.upsert(opp)
        fake_repos.drafts.upsert(my_draft)
        fake_repos.drafts.upsert(other_draft)
        _register_student_user(fake_repos, auth_service, my_student)
        _, notifier = auth_service
        _login(ui_client, notifier, my_student.email, get_settings().auth_local_demo_password)
        resp = ui_client.get("/app/student")
        assert resp.status_code == 200
        assert "Only for Other Person" not in resp.text
        assert "Private message for Other Person" not in resp.text

    def test_student_app_redirect_goes_to_student_dashboard(self, ui_client, fake_repos, auth_service):
        """GET /app for a student role redirects to /app/student."""
        student = _make_student()
        fake_repos.students.upsert(student)
        _register_student_user(fake_repos, auth_service, student)
        _, notifier = auth_service
        _login(ui_client, notifier, student.email, get_settings().auth_local_demo_password)
        resp = ui_client.get("/app", follow_redirects=False)
        assert resp.status_code == 303
        assert "student" in resp.headers["location"]


# ===========================================================================
# UAT-STU-04  Outcome Tracking
# ===========================================================================


class TestStudentOutcomeTracking:
    def _login_student(self, ui_client, fake_repos, auth_service, student, opp, draft):
        fake_repos.students.upsert(student)
        fake_repos.opportunities.upsert(opp)
        fake_repos.drafts.upsert(draft)
        _register_student_user(fake_repos, auth_service, student)
        _, notifier = auth_service
        _login(ui_client, notifier, student.email, get_settings().auth_local_demo_password)

    def test_student_can_save_applied_outcome(self, ui_client, fake_repos, auth_service):
        """Student marks an opportunity as 'applied'; the record persists."""
        student = _make_student()
        opp = _make_opp()
        draft = _make_draft(student, opp, status=DraftStatus.SENT)
        self._login_student(ui_client, fake_repos, auth_service, student, opp, draft)
        resp = ui_client.post(
            "/student/outcome",
            data={"opp_id": opp.id, "status": "applied", "notes": "Submitted my resume"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        updated = fake_repos.students.get(student.id)
        assert updated is not None
        assert any(o["opp_id"] == opp.id and o["status"] == "applied" for o in updated.outcomes)

    def test_student_can_update_outcome_status(self, ui_client, fake_repos, auth_service):
        """Student upgrades from 'applied' to 'interview'; the old record is replaced."""
        student = _make_student()
        # Pre-seed an existing 'applied' outcome
        student.outcomes = [{"opp_id": "opp_test", "status": "applied", "notes": "", "updated_at": "2026-06-01T00:00:00"}]
        opp = _make_opp()
        draft = _make_draft(student, opp, status=DraftStatus.SENT)
        self._login_student(ui_client, fake_repos, auth_service, student, opp, draft)
        ui_client.post(
            "/student/outcome",
            data={"opp_id": opp.id, "status": "interview", "notes": "Interview on June 15"},
            follow_redirects=False,
        )
        updated = fake_repos.students.get(student.id)
        assert updated is not None
        outcomes_for_opp = [o for o in updated.outcomes if o["opp_id"] == opp.id]
        assert len(outcomes_for_opp) == 1  # no duplicates
        assert outcomes_for_opp[0]["status"] == "interview"

    def test_outcome_without_status_clears_entry(self, ui_client, fake_repos, auth_service):
        """Submitting an empty status removes the outcome record for that opportunity."""
        student = _make_student()
        student.outcomes = [{"opp_id": "opp_test", "status": "applied", "notes": "", "updated_at": "2026-06-01"}]
        opp = _make_opp()
        draft = _make_draft(student, opp, status=DraftStatus.SENT)
        self._login_student(ui_client, fake_repos, auth_service, student, opp, draft)
        ui_client.post(
            "/student/outcome",
            data={"opp_id": opp.id, "status": "", "notes": ""},
            follow_redirects=False,
        )
        updated = fake_repos.students.get(student.id)
        assert updated is not None
        assert not any(o["opp_id"] == opp.id for o in updated.outcomes)

    def test_outcome_save_requires_auth(self, ui_client):
        """Unauthenticated POST to /student/outcome redirects to login."""
        resp = ui_client.post(
            "/student/outcome",
            data={"opp_id": "opp_test", "status": "applied", "notes": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "login" in resp.headers.get("location", "").lower()


# ===========================================================================
# UAT-STU-05  Profile Management
# ===========================================================================


class TestStudentProfileManagement:
    def _login_student(self, ui_client, fake_repos, auth_service, student):
        fake_repos.students.upsert(student)
        _register_student_user(fake_repos, auth_service, student)
        _, notifier = auth_service
        _login(ui_client, notifier, student.email, get_settings().auth_local_demo_password)

    def test_profile_page_loads_for_authenticated_student(self, ui_client, fake_repos, auth_service):
        """Authenticated student can access the profile page."""
        student = _make_student()
        self._login_student(ui_client, fake_repos, auth_service, student)
        resp = ui_client.get("/profile")
        assert resp.status_code == 200
        assert student.name in resp.text or "profile" in resp.text.lower()

    def test_profile_page_blocked_for_unauthenticated(self, ui_client):
        """Unauthenticated GET /profile redirects to login."""
        resp = ui_client.get("/profile", follow_redirects=False)
        assert resp.status_code == 303

    def test_student_can_update_career_interests(self, ui_client, fake_repos, auth_service):
        """Student saves new career interests; changes are persisted."""
        student = _make_student()
        self._login_student(ui_client, fake_repos, auth_service, student)
        resp = ui_client.post(
            "/profile",
            data={
                "career_interests": ["robotics", "hardware"],
                "opportunity_types": ["internship"],
                "notification_frequency": "weekly",
                "preferred_notification_method": "email",
                "phone": "",
                "bio": "Now into robotics.",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        updated = fake_repos.students.get(student.id)
        assert updated is not None
        assert "robotics" in updated.career_interests

    def test_student_can_update_notification_to_whatsapp(self, ui_client, fake_repos, auth_service):
        """Student switches notification preference to WhatsApp; change is saved."""
        student = _make_student()
        self._login_student(ui_client, fake_repos, auth_service, student)
        ui_client.post(
            "/profile",
            data={
                "career_interests": student.career_interests,
                "opportunity_types": student.opportunity_types_sought,
                "notification_frequency": "weekly",
                "preferred_notification_method": "whatsapp",
                "phone": "+16175550100",
                "bio": student.bio,
            },
            follow_redirects=False,
        )
        updated = fake_repos.students.get(student.id)
        assert updated is not None
        assert updated.preferred_notification_method == "whatsapp"
        assert updated.phone == "+16175550100"

    def test_student_can_update_bio(self, ui_client, fake_repos, auth_service):
        """Student edits their bio; the new text is persisted."""
        student = _make_student()
        self._login_student(ui_client, fake_repos, auth_service, student)
        ui_client.post(
            "/profile",
            data={
                "career_interests": student.career_interests,
                "opportunity_types": student.opportunity_types_sought,
                "notification_frequency": "weekly",
                "preferred_notification_method": "email",
                "phone": "",
                "bio": "Updated bio with new interests.",
            },
            follow_redirects=False,
        )
        updated = fake_repos.students.get(student.id)
        assert updated is not None
        assert updated.bio == "Updated bio with new interests."

    def test_profile_save_redirects_to_dashboard(self, ui_client, fake_repos, auth_service):
        """After saving profile, student is redirected back to profile with confirmation."""
        student = _make_student()
        self._login_student(ui_client, fake_repos, auth_service, student)
        resp = ui_client.post(
            "/profile",
            data={
                "career_interests": ["ai"],
                "opportunity_types": ["internship"],
                "notification_frequency": "weekly",
                "preferred_notification_method": "email",
                "phone": "",
                "bio": "",
                "opted_in": "on",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/profile" in resp.headers.get("location", "")
        assert "flash=" in resp.headers.get("location", "")


# ===========================================================================
# UAT-STU-06  Opportunity Suggestion
# ===========================================================================


class TestStudentOpportunitySuggestion:
    def _login_student(self, ui_client, fake_repos, auth_service, student):
        fake_repos.students.upsert(student)
        _register_student_user(fake_repos, auth_service, student)
        _, notifier = auth_service
        _login(ui_client, notifier, student.email, get_settings().auth_local_demo_password)

    def test_suggest_page_loads_for_authenticated_student(self, ui_client, fake_repos, auth_service):
        """The opportunity suggestion page is accessible to logged-in students."""
        student = _make_student()
        self._login_student(ui_client, fake_repos, auth_service, student)
        resp = ui_client.get("/opportunities/suggest")
        assert resp.status_code == 200
        assert "suggest" in resp.text.lower() or "opportunity" in resp.text.lower()

    def test_suggest_page_blocked_for_unauthenticated(self, ui_client):
        resp = ui_client.get("/opportunities/suggest", follow_redirects=False)
        assert resp.status_code == 303

    def test_student_can_submit_opportunity_suggestion(self, ui_client, fake_repos, auth_service):
        """Student submits a suggestion; it is created with needs_review=True."""
        student = _make_student()
        self._login_student(ui_client, fake_repos, auth_service, student)
        resp = ui_client.post(
            "/opportunities/suggest",
            data={
                "title": "OpenAI Research Fellows Program",
                "organization": "OpenAI",
                "kind": "fellowship",
                "summary": "Fellowship for early-career AI researchers.",
                "url": "https://openai.com/research/fellows",
                "deadline_str": "2026-09-15",
                "location": "Remote",
                "eligibility": "BS or MS graduates.",
                "tags_raw": "ai, research, fellowship",
                "fields_raw": "computer science, machine learning",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        # Suggested opportunity must land in review queue
        all_opps = fake_repos.opportunities.list_all()
        suggested = [o for o in all_opps if "OpenAI Research Fellows" in o.title]
        assert len(suggested) == 1
        assert suggested[0].needs_review is True

    def test_suggestion_submission_blocked_for_unauthenticated(self, ui_client):
        """Unauthenticated POST to /opportunities/suggest redirects to login."""
        resp = ui_client.post(
            "/opportunities/suggest",
            data={"title": "Fake", "organization": "Fake Org", "kind": "internship",
                  "summary": "Test", "url": "", "deadline_str": "", "location": "",
                  "eligibility": "", "tags_raw": "", "fields_raw": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "login" in resp.headers.get("location", "").lower()


# ===========================================================================
# UAT-STU-07  Role Isolation — Students Cannot Access Staff Routes
# ===========================================================================


class TestStudentRoleIsolation:
    def _login_student(self, ui_client, fake_repos, auth_service, student):
        fake_repos.students.upsert(student)
        _register_student_user(fake_repos, auth_service, student)
        _, notifier = auth_service
        _login(ui_client, notifier, student.email, get_settings().auth_local_demo_password)

    def test_student_cannot_access_admin_dashboard(self, ui_client, fake_repos, auth_service):
        """Students are redirected away from the admin dashboard."""
        student = _make_student()
        self._login_student(ui_client, fake_repos, auth_service, student)
        resp = ui_client.get("/app/admin", follow_redirects=False)
        assert resp.status_code == 303

    def test_student_cannot_access_ngo_dashboard(self, ui_client, fake_repos, auth_service):
        """Students are redirected away from the NGO dashboard."""
        student = _make_student()
        self._login_student(ui_client, fake_repos, auth_service, student)
        resp = ui_client.get("/app/ngo", follow_redirects=False)
        assert resp.status_code == 303

    def test_student_cannot_access_opportunities_catalogue(self, ui_client, fake_repos, auth_service):
        """Students cannot browse the full staff-facing opportunities catalogue."""
        student = _make_student()
        self._login_student(ui_client, fake_repos, auth_service, student)
        resp = ui_client.get("/opportunities", follow_redirects=False)
        assert resp.status_code == 303

    def test_student_cannot_access_students_roster(self, ui_client, fake_repos, auth_service):
        """Students cannot access the student roster — it is staff-only."""
        student = _make_student()
        self._login_student(ui_client, fake_repos, auth_service, student)
        resp = ui_client.get("/students", follow_redirects=False)
        assert resp.status_code == 303

    def test_student_cannot_approve_drafts(self, ui_client, fake_repos, auth_service, pending_draft):
        """A student cannot approve another user's draft via the UI endpoint."""
        student = _make_student()
        fake_repos.drafts.upsert(pending_draft)
        self._login_student(ui_client, fake_repos, auth_service, student)
        resp = ui_client.post(
            f"/ui/drafts/{pending_draft.id}/approve",
            follow_redirects=False,
        )
        assert resp.status_code in (303, 401, 403)

    def test_student_cannot_access_kpi_dashboard(self, ui_client, fake_repos, auth_service):
        """KPI/admin dashboard is inaccessible to students."""
        student = _make_student()
        self._login_student(ui_client, fake_repos, auth_service, student)
        resp = ui_client.get("/admin/kpi", follow_redirects=False)
        assert resp.status_code == 303


# ===========================================================================
# UAT-STU-08  Password Reset Flow
# ===========================================================================


class TestPasswordReset:
    def test_forgot_page_renders(self, ui_client):
        """The password reset page is accessible."""
        resp = ui_client.get("/forgot")
        assert resp.status_code == 200

    def test_forgot_sends_reset_code(self, ui_client, fake_repos, auth_service):
        """Submitting a valid email triggers a reset code notification."""
        student = _make_student()
        fake_repos.students.upsert(student)
        service, notifier = auth_service
        _register_student_user(fake_repos, auth_service, student)
        resp = ui_client.post("/auth/forgot", data={"email": student.email})
        assert resp.status_code == 200
        assert student.email in notifier.codes

    def test_reset_with_valid_code_changes_password(self, ui_client, fake_repos, auth_service):
        """Student uses the reset code to set a new password successfully."""
        student = _make_student()
        fake_repos.students.upsert(student)
        _register_student_user(fake_repos, auth_service, student)
        _, notifier = auth_service
        ui_client.post("/auth/forgot", data={"email": student.email})
        code = notifier.codes[student.email]
        resp = ui_client.post(
            "/auth/reset",
            data={
                "email": student.email,
                "code": code,
                "new_access_key": "BrandNew99!",  # field name matches the route param
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
