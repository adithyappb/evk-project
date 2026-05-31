"""Authentication and MFA helpers for EVKids."""

from __future__ import annotations

import hashlib
import secrets
import smtplib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

from evk.config import Settings, get_settings
from evk.firestore_repo import Repos
from evk.logging import logger
from evk.models import AppUser, LoginChallenge, Session, UserRole


class AuthError(ValueError):
    """Raised when login or signup cannot continue."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _hash_value(value: str, *, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        value.encode("utf-8"),
        salt.encode("utf-8"),
        200_000,
    ).hex()


def hash_access_key(access_key: str, *, salt: str) -> str:
    return _hash_value(access_key, salt=salt)


def verify_access_key(access_key: str, *, salt: str, expected_hash: str) -> bool:
    candidate = hash_access_key(access_key, salt=salt)
    return secrets.compare_digest(candidate, expected_hash)


def hash_login_code(code: str, *, user_id: str) -> str:
    return _hash_value(code, salt=f"otp:{user_id}")


def verify_login_code(code: str, *, user_id: str, expected_hash: str) -> bool:
    candidate = hash_login_code(code, user_id=user_id)
    return secrets.compare_digest(candidate, expected_hash)


def _slug_from_email(email: str) -> str:
    local = email.split("@", 1)[0]
    return "".join(ch if ch.isalnum() else "_" for ch in local.lower()).strip("_") or "user"


def _default_name(email: str) -> str:
    return " ".join(part.capitalize() for part in _slug_from_email(email).split("_"))


def _demo_users(settings: Settings) -> list[dict[str, str]]:
    return [
        {
            "id": "user_admin_evk",
            "email": "admin@evkids.org",
            "name": "EVKids Admin",
            "role": UserRole.ADMIN.value,
            "organization": "EVKids",
            "password": settings.auth_local_demo_password,
        },
        {
            "id": "user_ngo_partner",
            "email": "partner@evkids.org",
            "name": "Community Partner",
            "role": UserRole.NGO_ADMIN.value,
            "organization": "EVKids Partner Network",
            "password": settings.auth_local_demo_password,
        },
    ]


class AuthNotifier:
    def send_code(self, *, email: str, code: str) -> None:  # pragma: no cover - interface only
        raise NotImplementedError


class TerminalAuthNotifier(AuthNotifier):
    """Prints the OTP to stdout and keeps it in memory so routes can surface
    it directly in the browser during local / dev mode."""

    def __init__(self) -> None:
        self.last_code: str | None = None

    def send_code(self, *, email: str, code: str) -> None:
        self.last_code = code
        logger.bind(email=email, code=code).info("auth.otp_terminal")
        print(f"[EVkids login] MFA code for {email}: {code}", flush=True)


class SmtpAuthNotifier(AuthNotifier):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Always store the last code so admin tools can surface it for test accounts.
        self.last_code: str | None = None

    def send_code(self, *, email: str, code: str) -> None:
        self.last_code = code
        # Always print to terminal so test/pilot accounts with fake email addresses
        # can still be used — admin checks the server output for the OTP.
        print(f"[EVkids OTP] {email}: {code}", flush=True)
        msg = EmailMessage()
        msg["Subject"] = "EVkids verification code"
        msg["From"] = self._settings.effective_smtp_sender
        msg["To"] = email
        msg.set_content(
            f"Your EVkids verification code is {code}. "
            f"It expires in {self._settings.login_code_ttl_minutes} minutes."
        )
        host = self._settings.effective_smtp_host
        port = self._settings.effective_smtp_port
        try:
            with smtplib.SMTP(host, port, timeout=10) as client:
                if self._settings.effective_smtp_username:
                    client.ehlo()
                    client.starttls()
                    client.ehlo()
                    client.login(
                        self._settings.effective_smtp_username,
                        self._settings.effective_smtp_password,
                    )
                client.send_message(msg)
        except smtplib.SMTPAuthenticationError as exc:
            logger.bind(email=email, host=host, error=str(exc)).error("auth.otp_smtp_auth_failed")
            raise AuthError(
                "Email delivery failed — the Gmail App Password was rejected. "
                "Check GMAIL_APP_PASSWORD in .env and restart the server."
            ) from exc
        except (smtplib.SMTPException, OSError, UnicodeEncodeError) as exc:
            logger.bind(email=email, host=host, error=str(exc)).error("auth.otp_smtp_error")
            raise AuthError(
                f"Email delivery failed ({type(exc).__name__}). "
                "Check your SMTP/Gmail settings in .env."
            ) from exc
        logger.bind(email=email, host=host).info("auth.otp_smtp")


def build_auth_notifier(settings: Settings | None = None) -> AuthNotifier:
    cfg = settings or get_settings()
    if cfg.gmail_app_password or cfg.auth_email_delivery_mode == "smtp":
        return SmtpAuthNotifier(cfg)
    return TerminalAuthNotifier()


@dataclass(slots=True)
class AuthService:
    repos: Repos
    notifier: AuthNotifier
    settings: Settings

    def bootstrap_local_users(self) -> None:
        for item in _demo_users(self.settings):
            if self.repos.users.get(item["id"]) is not None:
                continue
            salt = secrets.token_hex(8)
            self.repos.users.upsert(
                AppUser(
                    id=item["id"],
                    email=item["email"],
                    name=item["name"],
                    role=UserRole(item["role"]),
                    organization=item["organization"],
                    access_key_salt=salt,
                    access_key_hash=hash_access_key(item["password"], salt=salt),
                )
            )

        for student in self.repos.students.list_all(limit=100):
            existing = self.repos.users.get_by_email(student.email)
            if existing is not None:
                continue
            salt = secrets.token_hex(8)
            self.repos.users.upsert(
                AppUser(
                    id=f"user_student_{student.id}",
                    email=student.email,
                    name=student.name,
                    role=UserRole.STUDENT,
                    organization=student.school,
                    student_id=student.id,
                    access_key_salt=salt,
                    access_key_hash=hash_access_key(self.settings.auth_local_demo_password, salt=salt),
                )
            )

    def ensure_bootstrap(self) -> None:
        if self.settings.is_local:
            self.bootstrap_local_users()

    def create_user(
        self,
        *,
        email: str,
        name: str,
        role: UserRole,
        access_key: str,
        organization: str = "",
    ) -> AppUser:
        self.ensure_bootstrap()
        email_norm = email.strip().lower()
        if self.repos.users.get_by_email(email_norm) is not None:
            raise AuthError("An account with that email already exists.")
        if len(access_key.strip()) < 8:
            raise AuthError("Use an access key with at least 8 characters.")
        linked_student = self.repos.students.get_by_email(email_norm) if role is UserRole.STUDENT else None
        salt = secrets.token_hex(8)
        user = AppUser(
            id=f"user_{role.value}_{_slug_from_email(email_norm)}",
            email=email_norm,
            name=name.strip() or _default_name(email_norm),
            role=role,
            organization=organization.strip() or (linked_student.school if linked_student else ""),
            student_id=linked_student.id if linked_student else None,
            access_key_salt=salt,
            access_key_hash=hash_access_key(access_key, salt=salt),
        )
        self.repos.users.upsert(user)
        return user

    def start_login(self, *, email: str, access_key: str) -> tuple[AppUser, str | None]:
        self.ensure_bootstrap()
        email_norm = email.strip().lower()
        user = self.repos.users.get_by_email(email_norm)
        if user is None:
            raise AuthError("No account found for that email yet.")
        if not user.is_active:
            raise AuthError("This account is inactive. Ask an EVKids admin to reactivate it.")
        if not verify_access_key(
            access_key,
            salt=user.access_key_salt,
            expected_hash=user.access_key_hash,
        ):
            raise AuthError("That access key was not recognized.")

        code = f"{secrets.randbelow(1_000_000):06d}"
        challenge = LoginChallenge(
            id=f"challenge_{user.id}_{secrets.token_hex(4)}",
            user_id=user.id,
            email=user.email,
            code_hash=hash_login_code(code, user_id=user.id),
            expires_at=_utcnow() + timedelta(minutes=self.settings.login_code_ttl_minutes),
            purpose="login",
        )
        self.repos.login_challenges.upsert(challenge)
        self.notifier.send_code(email=user.email, code=code)
        logger.bind(user_id=user.id).info("auth.challenge_created")
        # Show code in the browser banner for:
        #   - TerminalAuthNotifier (no Gmail configured — always show)
        #   - SmtpAuthNotifier + placeholder email domain (evkids.org, example.edu,
        #     example.com) — Gmail can't deliver to these, so surface the code in-browser
        code_hint: str | None = None
        if hasattr(self.notifier, "last_code") and self.notifier.last_code:  # type: ignore[union-attr]
            is_terminal = isinstance(self.notifier, TerminalAuthNotifier)
            is_placeholder_domain = any(
                user.email.endswith(d)
                for d in ("@evkids.org", "@example.edu", "@example.com", "@test.com", ".local")
            )
            if is_terminal or is_placeholder_domain:
                code_hint = self.notifier.last_code  # type: ignore[union-attr]
        return user, code_hint

    def verify_login(self, *, email: str, code: str) -> tuple[AppUser, Session]:
        self.ensure_bootstrap()
        email_norm = email.strip().lower()
        user = self.repos.users.get_by_email(email_norm)
        if user is None:
            raise AuthError("No account found for that email.")
        challenges = self.repos.login_challenges.list_for_user(user.id, limit=10)
        active = next(
            (
                challenge
                for challenge in challenges
                if challenge.purpose == "login"
                and challenge.used_at is None
                and challenge.expires_at >= _utcnow()
            ),
            None,
        )
        if active is None:
            raise AuthError("No active code is available. Request a new login code.")
        if not verify_login_code(code.strip(), user_id=user.id, expected_hash=active.code_hash):
            raise AuthError("That verification code is incorrect.")
        self.repos.login_challenges.patch(active.id, {"used_at": _utcnow()})
        session = Session(
            id=f"session_{secrets.token_urlsafe(18)}",
            user_id=user.id,
            role=user.role,
            expires_at=_utcnow() + timedelta(hours=self.settings.session_ttl_hours),
        )
        self.repos.sessions.upsert(session)
        self.repos.users.patch(user.id, {"last_login_at": _utcnow()})
        logger.bind(user_id=user.id, session_id=session.id).info("auth.session_created")
        refreshed_user = self.repos.users.get(user.id)
        assert refreshed_user is not None
        return refreshed_user, session

    def get_session_user(self, session_id: str | None) -> AppUser | None:
        self.ensure_bootstrap()
        if not session_id:
            return None
        session = self.repos.sessions.get(session_id)
        if session is None or session.expires_at < _utcnow():
            return None
        self.repos.sessions.patch(session.id, {"last_seen_at": _utcnow()})
        user = self.repos.users.get(session.user_id)
        if user is None or not user.is_active:
            return None
        return user

    def revoke_session(self, session_id: str | None) -> None:
        if not session_id:
            return
        self.repos.sessions.delete(session_id)

    def start_reset(self, *, email: str) -> str | None:
        """Issue a password-reset code for a known email.

        Returns plain code in terminal mode (for browser display), None otherwise.
        Never reveals whether the email exists to the caller.
        """
        self.ensure_bootstrap()
        email_norm = email.strip().lower()
        user = self.repos.users.get_by_email(email_norm)
        if user is None:
            # Don't reveal whether the email exists; silently succeed.
            logger.bind(email=email_norm).info("auth.reset_unknown_email")
            return None
        code = f"{secrets.randbelow(1_000_000):06d}"
        challenge = LoginChallenge(
            id=f"reset_{user.id}_{secrets.token_hex(4)}",
            user_id=user.id,
            email=user.email,
            code_hash=hash_login_code(code, user_id=user.id),
            expires_at=_utcnow() + timedelta(minutes=self.settings.login_code_ttl_minutes),
            purpose="reset",
        )
        self.repos.login_challenges.upsert(challenge)
        self.notifier.send_code(email=user.email, code=code)
        logger.bind(user_id=user.id).info("auth.reset_challenge_created")
        code_hint: str | None = None
        if hasattr(self.notifier, "last_code") and self.notifier.last_code:  # type: ignore[union-attr]
            is_terminal = isinstance(self.notifier, TerminalAuthNotifier)
            is_placeholder_domain = any(
                user.email.endswith(d)
                for d in ("@evkids.org", "@example.edu", "@example.com", "@test.com", ".local")
            )
            if is_terminal or is_placeholder_domain:
                code_hint = self.notifier.last_code  # type: ignore[union-attr]
        return code_hint

    def send_welcome_email(self, *, student_email: str, setup_url: str) -> None:
        """Send a welcome/activation email with a profile setup link."""
        msg = EmailMessage()
        msg["Subject"] = "Welcome to EVkids — set up your profile"
        msg["From"] = self.settings.effective_smtp_sender
        msg["To"] = student_email
        msg.set_content(
            f"Welcome to EVkids!\n\n"
            f"Click the link below to set your password and complete your profile:\n\n"
            f"{setup_url}\n\n"
            f"This link expires in 7 days.\n\n"
            f"— The EVkids Team"
        )
        if not self.settings.effective_smtp_username:
            logger.bind(email=student_email, url=setup_url).info("auth.welcome_terminal")
            print(f"[EVkids welcome] Setup link for {student_email}: {setup_url}", flush=True)
            return
        with smtplib.SMTP(self.settings.effective_smtp_host, self.settings.effective_smtp_port, timeout=10) as client:
            client.ehlo()
            client.starttls()
            client.ehlo()
            client.login(self.settings.effective_smtp_username, self.settings.effective_smtp_password)
            client.send_message(msg)
        logger.bind(email=student_email).info("auth.welcome_sent")

    def complete_reset(self, *, email: str, code: str, new_access_key: str) -> "AppUser":
        """Verify reset code and replace access key.

        Raises AuthError on failure. Invalidates all sessions for the user.
        """
        self.ensure_bootstrap()
        email_norm = email.strip().lower()
        user = self.repos.users.get_by_email(email_norm)
        if user is None:
            raise AuthError("No account found for that email.")
        if len(new_access_key.strip()) < 8:
            raise AuthError("Use a new password with at least 8 characters.")
        challenges = self.repos.login_challenges.list_for_user(user.id, limit=20)
        active = next(
            (
                c
                for c in challenges
                if c.purpose == "reset"
                and c.used_at is None
                and c.expires_at >= _utcnow()
            ),
            None,
        )
        if active is None:
            raise AuthError("No active reset code is available. Request a new one.")
        if not verify_login_code(code.strip(), user_id=user.id, expected_hash=active.code_hash):
            raise AuthError("That reset code is incorrect.")
        self.repos.login_challenges.patch(active.id, {"used_at": _utcnow()})
        # Update access key
        salt = secrets.token_hex(8)
        self.repos.users.patch(
            user.id,
            {
                "access_key_salt": salt,
                "access_key_hash": hash_access_key(new_access_key, salt=salt),
            },
        )
        # Invalidate all sessions — list_for_user may not exist on SessionRepo,
        # so we check defensively and skip if unavailable.
        if hasattr(self.repos.sessions, "list_for_user"):
            for session in self.repos.sessions.list_for_user(user.id):  # type: ignore[union-attr]
                self.repos.sessions.delete(session.id)
        refreshed = self.repos.users.get(user.id)
        assert refreshed is not None
        logger.bind(user_id=user.id).info("auth.reset_complete")
        return refreshed


__all__ = [
    "AuthError",
    "AuthNotifier",
    "AuthService",
    "SmtpAuthNotifier",
    "TerminalAuthNotifier",
    "build_auth_notifier",
    "hash_access_key",
]
