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
    def send_code(self, *, email: str, code: str) -> None:
        logger.bind(email=email, code=code).info("auth.otp_terminal")
        print(f"[EVKids login] MFA code for {email}: {code}", flush=True)


class SmtpAuthNotifier(AuthNotifier):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def send_code(self, *, email: str, code: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = "EVKids verification code"
        msg["From"] = self._settings.auth_smtp_sender
        msg["To"] = email
        msg.set_content(
            "Your EVKids verification code is "
            f"{code}. It expires in {self._settings.login_code_ttl_minutes} minutes."
        )
        with smtplib.SMTP(self._settings.auth_smtp_host, self._settings.auth_smtp_port) as client:
            if self._settings.auth_smtp_username:
                client.starttls()
                client.login(
                    self._settings.auth_smtp_username,
                    self._settings.auth_smtp_password,
                )
            client.send_message(msg)
        logger.bind(email=email, host=self._settings.auth_smtp_host).info("auth.otp_smtp")


def build_auth_notifier(settings: Settings | None = None) -> AuthNotifier:
    cfg = settings or get_settings()
    if cfg.auth_email_delivery_mode == "smtp":
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

    def start_login(self, *, email: str, access_key: str) -> AppUser:
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
        )
        self.repos.login_challenges.upsert(challenge)
        self.notifier.send_code(email=user.email, code=code)
        logger.bind(user_id=user.id).info("auth.challenge_created")
        return user

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
                if challenge.used_at is None and challenge.expires_at >= _utcnow()
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


__all__ = [
    "AuthError",
    "AuthNotifier",
    "AuthService",
    "SmtpAuthNotifier",
    "TerminalAuthNotifier",
    "build_auth_notifier",
    "hash_access_key",
]
