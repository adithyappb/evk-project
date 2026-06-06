"""In-memory fakes for Firestore repos, Inkbox, and Gemini.

These are drop-in substitutes usable anywhere a real client is expected.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from evk.firestore_repo import (
    DraftRepo,
    LoginChallengeRepo,
    OpportunityRepo,
    RawEmailRepo,
    ReminderRepo,
    Repos,
    SessionRepo,
    StudentRepo,
    UserRepo,
)
from evk.inkbox_client import InboundMessage, InkboxClient
from evk.models import (
    AppUser,
    DraftMessage,
    DraftStatus,
    LoginChallenge,
    Opportunity,
    RawEmailStatus,
    Student,
)

# --------------------------------------------------------------------------- #
# Fake repos — subclass the real ones so isinstance holds; skip super.__init__ #
# --------------------------------------------------------------------------- #


class _FakeRepoMixin:
    """Shared in-memory storage + CRUD behaviour for all fake repos."""

    def __init__(self) -> None:
        self._docs: dict[str, Any] = {}

    @property
    def collection(self) -> Any:  # pragma: no cover - never hit in tests
        raise AssertionError("Fake repos don't expose a Firestore collection")

    def get(self, doc_id: str):
        return self._clone(self._docs.get(doc_id))

    def upsert(self, doc):
        self._docs[doc.id] = self._clone(doc)
        return doc

    def upsert_many(self, docs, *, batch_size: int = 500):
        for d in docs:
            self._docs[d.id] = self._clone(d)
        return len(docs)

    def create_if_absent(self, doc):
        if doc.id in self._docs:
            return self._clone(self._docs[doc.id]), False
        self._docs[doc.id] = self._clone(doc)
        return doc, True

    def patch(self, doc_id: str, fields: dict[str, Any]) -> None:
        if doc_id not in self._docs:
            raise KeyError(doc_id)
        current = self._docs[doc_id].model_dump()
        current.update(fields)
        model_cls = type(self._docs[doc_id])
        self._docs[doc_id] = model_cls.model_validate(current)

    def delete(self, doc_id: str) -> None:
        self._docs.pop(doc_id, None)

    def list_all(self, limit: int | None = None):
        out = [self._clone(v) for v in self._docs.values()]
        return out[:limit] if limit else out

    @staticmethod
    def _clone(obj):
        if obj is None:
            return None
        return obj.__class__.model_validate(obj.model_dump())


class FakeStudentRepo(_FakeRepoMixin, StudentRepo):
    def list_opted_in(self) -> list[Student]:
        return [s for s in self.list_all() if s.opted_in]

    def get_by_email(self, email: str) -> Student | None:
        email_norm = email.strip().lower()
        for student in self._docs.values():
            if student.email.lower() == email_norm:
                return self._clone(student)
        return None


class FakeOpportunityRepo(_FakeRepoMixin, OpportunityRepo):
    def with_upcoming_deadlines(self, *, within_days: int) -> list[Opportunity]:
        now = datetime.now(UTC)
        cutoff = now.timestamp() + within_days * 86_400
        upcoming = [
            o
            for o in self.list_all()
            if o.deadline and now.timestamp() <= o.deadline.timestamp() <= cutoff
        ]
        upcoming.sort(key=lambda o: o.deadline)  # type: ignore[arg-type, return-value]
        return upcoming


class FakeRawEmailRepo(_FakeRepoMixin, RawEmailRepo):
    def exists(self, inkbox_message_id: str) -> bool:
        return inkbox_message_id in self._docs

    def mark_status(
        self,
        doc_id: str,
        status: RawEmailStatus,
        *,
        extracted_opportunity_ids: list[str] | None = None,
        error: str | None = None,
    ) -> None:
        patch: dict[str, Any] = {"status": status.value}
        if extracted_opportunity_ids is not None:
            patch["extracted_opportunity_ids"] = extracted_opportunity_ids
        if error is not None:
            patch["classification_error"] = error
        self.patch(doc_id, patch)


class FakeDraftRepo(_FakeRepoMixin, DraftRepo):
    def list_by_status(self, status: DraftStatus, limit: int = 200) -> list[DraftMessage]:
        items = [d for d in self.list_all() if d.status == status]
        items.sort(key=lambda d: d.created_at, reverse=True)
        return items[:limit]

    def exists_for_pair(self, student_id: str, opportunity_id: str) -> bool:
        return any(
            d.student_id == student_id and d.opportunity_id == opportunity_id
            for d in self._docs.values()
        )


class FakeReminderRepo(_FakeRepoMixin, ReminderRepo):
    def exists(self, student_id: str, opportunity_id: str, days_before: int) -> bool:
        return any(
            r.student_id == student_id
            and r.opportunity_id == opportunity_id
            and r.days_before == days_before
            for r in self._docs.values()
        )


class FakeUserRepo(_FakeRepoMixin, UserRepo):
    def get_by_email(self, email: str) -> AppUser | None:
        email_norm = email.strip().lower()
        for user in self._docs.values():
            if user.email.lower() == email_norm:
                return self._clone(user)
        return None


class FakeLoginChallengeRepo(_FakeRepoMixin, LoginChallengeRepo):
    def list_for_user(self, user_id: str, limit: int = 20) -> list[LoginChallenge]:
        items = [challenge for challenge in self.list_all() if challenge.user_id == user_id]
        items.sort(key=lambda challenge: challenge.created_at, reverse=True)
        return items[:limit]


class FakeSessionRepo(_FakeRepoMixin, SessionRepo):
    pass


def build_fake_repos() -> Repos:
    return Repos(
        students=FakeStudentRepo(),
        opportunities=FakeOpportunityRepo(),
        raw_emails=FakeRawEmailRepo(),
        drafts=FakeDraftRepo(),
        reminders=FakeReminderRepo(),
        users=FakeUserRepo(),
        login_challenges=FakeLoginChallengeRepo(),
        sessions=FakeSessionRepo(),
    )


# --------------------------------------------------------------------------- #
# Fake Inkbox                                                                  #
# --------------------------------------------------------------------------- #


@dataclass
class SentEmail:
    to: list[str]
    subject: str
    body_text: str
    body_html: str | None = None
    in_reply_to_message_id: str | None = None
    cc: list[str] | None = None
    bcc: list[str] | None = None


class FakeInkbox(InkboxClient):
    """No network. Tracks sends in ``.sent``; feeds inbound from ``.inbound_queue``."""

    def __init__(self, *, inbound: list[InboundMessage] | None = None) -> None:
        self.sent: list[SentEmail] = []
        self.inbound_queue: list[InboundMessage] = list(inbound or [])
        self.marked_read: list[str] = []
        self._next_id = 1

    def send(  # type: ignore[override]
        self,
        *,
        to: list[str],
        subject: str,
        body_text: str,
        body_html: str | None = None,
        in_reply_to_message_id: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
    ) -> str:
        self.sent.append(
            SentEmail(
                to=to,
                subject=subject,
                body_text=body_text,
                body_html=body_html,
                in_reply_to_message_id=in_reply_to_message_id,
                cc=cc,
                bcc=bcc,
            )
        )
        mid = f"msg_fake_{self._next_id}"
        self._next_id += 1
        return mid

    def iter_inbound(self) -> Iterable[InboundMessage]:  # type: ignore[override]
        yield from self.inbound_queue

    def iter_unread_inbound(self) -> Iterable[InboundMessage]:  # type: ignore[override]
        yield from self.inbound_queue

    def mark_read(self, message_ids: list[str]) -> None:  # type: ignore[override]
        self.marked_read.extend(message_ids)

    def fetch(self, message_id: str) -> InboundMessage | None:  # type: ignore[override]
        for m in self.inbound_queue:
            if m.id == message_id:
                return m
        return None


# --------------------------------------------------------------------------- #
# Fake Gemini                                                                  #
# --------------------------------------------------------------------------- #


@dataclass
class _GenCall:
    prompt: str
    schema: type | None


class FakeGemini:
    """Scriptable Gemini. Queue responses with ``queue_structured`` / ``queue_text``.

    Thread-safe: pops are serialised under a lock, and responses are matched
    to the requested ``schema`` so interleaved calls (classify + personalise)
    can't cross-wire.
    """

    def __init__(self) -> None:
        self._structured_responses: list[Any] = []
        self._text_responses: list[str] = []
        self.calls: list[_GenCall] = []
        self._lock = threading.Lock()

    def queue_structured(self, response) -> None:
        self._structured_responses.append(response)

    def queue_text(self, text: str) -> None:
        self._text_responses.append(text)

    def generate_structured(
        self,
        *,
        prompt: str,
        schema: type,
        system_instruction: str | None = None,
        temperature: float = 0.1,
        max_output_tokens: int = 4096,
    ):
        with self._lock:
            self.calls.append(_GenCall(prompt=prompt, schema=schema))
            if not self._structured_responses:
                raise AssertionError(f"FakeGemini has no queued structured response for {schema}")
            # Prefer a schema-compatible queued response; fall back to FIFO.
            match_idx = None
            for i, r in enumerate(self._structured_responses):
                if isinstance(r, schema):
                    match_idx = i
                    break
                if isinstance(r, dict):
                    try:
                        schema.model_validate(r)
                        match_idx = i
                        break
                    except Exception:
                        continue
            response = self._structured_responses.pop(match_idx if match_idx is not None else 0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, dict):
            return schema.model_validate(response)
        return response

    @staticmethod
    def healthcheck() -> bool:
        return True

    def generate_text(
        self,
        *,
        prompt: str,
        system_instruction: str | None = None,
        temperature: float = 0.3,
        max_output_tokens: int = 2048,
    ) -> str:
        with self._lock:
            self.calls.append(_GenCall(prompt=prompt, schema=None))
            if not self._text_responses:
                raise AssertionError("FakeGemini has no queued text response")
            return self._text_responses.pop(0)


__all__ = [
    "FakeDraftRepo",
    "FakeGemini",
    "FakeInkbox",
    "FakeOpportunityRepo",
    "FakeRawEmailRepo",
    "FakeReminderRepo",
    "FakeStudentRepo",
    "SentEmail",
    "build_fake_repos",
]
