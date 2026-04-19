"""Typed Firestore repositories for each collection.

Each repository exposes narrow, typed CRUD methods; callers never see raw
`DocumentReference` or dict-shaped payloads. All writes go through Pydantic
models so the on-disk shape is always valid.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, Generic, TypeVar

from google.cloud import firestore
from google.cloud.firestore_v1 import FieldFilter

from evk.config import get_settings
from evk.logging import logger
from evk.models import (
    DraftMessage,
    DraftStatus,
    FirestoreDoc,
    Opportunity,
    RawEmail,
    RawEmailStatus,
    ReminderLog,
    Student,
)

T = TypeVar("T", bound=FirestoreDoc)


# --------------------------------------------------------------------------- #
# Client                                                                      #
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def get_firestore_client() -> firestore.Client:
    """Return a process-singleton Firestore client."""
    settings = get_settings()
    return firestore.Client(
        project=settings.effective_firestore_project,
        database=settings.firestore_database,
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Base repository                                                             #
# --------------------------------------------------------------------------- #


class _Repo(Generic[T]):
    """Generic base — subclasses set `collection_name` and `model`."""

    collection_name: str
    model: type[T]

    def __init__(self, client: firestore.Client | None = None) -> None:
        self._client = client or get_firestore_client()

    @property
    def collection(self) -> firestore.CollectionReference:
        return self._client.collection(self.collection_name)

    # --- CRUD --------------------------------------------------------------

    def get(self, doc_id: str) -> T | None:
        snap = self.collection.document(doc_id).get()
        if not snap.exists:
            return None
        return self._load(snap.id, snap.to_dict() or {})

    # Firestore hard-limits a single batch to 500 operations.
    _BATCH_HARD_CAP = 500

    def upsert(self, doc: T) -> T:
        doc.updated_at = _utcnow()
        payload = self._dump(doc)
        self.collection.document(doc.id).set(payload, merge=False)
        logger.bind(collection=self.collection_name, id=doc.id).debug("firestore.upsert")
        return doc

    def upsert_many(self, docs: list[T], *, batch_size: int = 500) -> int:
        """Batch-upsert with Firestore's 500-op hard cap per commit.

        Returns the number of documents written. Safe to call with any list
        size; we chunk internally and commit each chunk atomically.
        """
        if not docs:
            return 0
        size = min(max(1, batch_size), self._BATCH_HARD_CAP)
        written = 0
        for start in range(0, len(docs), size):
            chunk = docs[start : start + size]
            batch = self._client.batch()
            for d in chunk:
                d.updated_at = _utcnow()
                batch.set(self.collection.document(d.id), self._dump(d), merge=False)
            batch.commit()
            written += len(chunk)
        logger.bind(collection=self.collection_name, count=written).info("firestore.upsert_many")
        return written

    def create_if_absent(self, doc: T) -> tuple[T, bool]:
        """Create the document only if it doesn't exist. Returns (doc, created)."""
        ref = self.collection.document(doc.id)
        snap = ref.get()
        if snap.exists:
            return self._load(snap.id, snap.to_dict() or {}), False
        self.upsert(doc)
        return doc, True

    def patch(self, doc_id: str, fields: dict[str, Any]) -> None:
        fields["updated_at"] = _utcnow()
        self.collection.document(doc_id).update(fields)

    def delete(self, doc_id: str) -> None:
        self.collection.document(doc_id).delete()

    def list_all(self, limit: int | None = None) -> list[T]:
        q: firestore.Query = self.collection
        if limit:
            q = q.limit(limit)
        return [self._load(s.id, s.to_dict() or {}) for s in q.stream()]

    # --- serialisation -----------------------------------------------------

    def _dump(self, doc: T) -> dict[str, Any]:
        """Pydantic → Firestore-safe dict (URLs and enums flattened to strings)."""
        return doc.model_dump(mode="json", exclude={"id"})

    def _load(self, doc_id: str, data: dict[str, Any]) -> T:
        return self.model.model_validate({"id": doc_id, **data})


# --------------------------------------------------------------------------- #
# Specific repositories                                                       #
# --------------------------------------------------------------------------- #


class StudentRepo(_Repo[Student]):
    collection_name = "students"
    model = Student

    def list_opted_in(self) -> list[Student]:
        q = self.collection.where(filter=FieldFilter("opted_in", "==", True))
        return [self._load(s.id, s.to_dict() or {}) for s in q.stream()]


class OpportunityRepo(_Repo[Opportunity]):
    collection_name = "opportunities"
    model = Opportunity

    @staticmethod
    def stable_id(*, title: str, deadline_iso: str | None, **_: object) -> str:
        """Deterministic ID so the same opportunity is never duplicated.

        Dedupes purely on normalized (title, deadline) — if two different
        newsletters mention the same opportunity they collapse to one record.
        Extra kwargs are accepted (and ignored) for backwards compatibility.
        """
        normalized = " ".join(title.strip().lower().split())
        key = f"{normalized}|{deadline_iso or ''}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]

    def with_upcoming_deadlines(self, *, within_days: int) -> list[Opportunity]:
        now = _utcnow()
        q = (
            self.collection.where(filter=FieldFilter("deadline", ">=", now))
            .order_by("deadline")
            .limit(500)
        )
        cutoff_ts = now.timestamp() + within_days * 86_400
        out: list[Opportunity] = []
        for snap in q.stream():
            opp = self._load(snap.id, snap.to_dict() or {})
            if opp.deadline and opp.deadline.timestamp() <= cutoff_ts:
                out.append(opp)
        return out


class RawEmailRepo(_Repo[RawEmail]):
    collection_name = "raw_emails"
    model = RawEmail

    def exists(self, inkbox_message_id: str) -> bool:
        """Idempotency check keyed on Inkbox's own message id."""
        ref = self.collection.document(inkbox_message_id)
        return ref.get().exists

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


class DraftRepo(_Repo[DraftMessage]):
    collection_name = "drafts"
    model = DraftMessage

    def list_by_status(self, status: DraftStatus, limit: int = 200) -> list[DraftMessage]:
        q = (
            self.collection.where(filter=FieldFilter("status", "==", status.value))
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        return [self._load(s.id, s.to_dict() or {}) for s in q.stream()]

    def exists_for_pair(self, student_id: str, opportunity_id: str) -> bool:
        q = (
            self.collection.where(filter=FieldFilter("student_id", "==", student_id))
            .where(filter=FieldFilter("opportunity_id", "==", opportunity_id))
            .limit(1)
        )
        return next(iter(q.stream()), None) is not None


class ReminderRepo(_Repo[ReminderLog]):
    collection_name = "reminder_logs"
    model = ReminderLog

    def exists(self, student_id: str, opportunity_id: str, days_before: int) -> bool:
        q = (
            self.collection.where(filter=FieldFilter("student_id", "==", student_id))
            .where(filter=FieldFilter("opportunity_id", "==", opportunity_id))
            .where(filter=FieldFilter("days_before", "==", days_before))
            .limit(1)
        )
        return next(iter(q.stream()), None) is not None


# --------------------------------------------------------------------------- #
# Convenience factory                                                         #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Repos:
    """Bundle of all repositories — inject this instead of individual repos.

    Uses a plain dataclass so test fakes can subclass each repo without
    triggering Pydantic arbitrary-type checks.
    """

    students: StudentRepo
    opportunities: OpportunityRepo
    raw_emails: RawEmailRepo
    drafts: DraftRepo
    reminders: ReminderRepo


@lru_cache(maxsize=1)
def get_repos() -> Repos:
    client = get_firestore_client()
    return Repos(
        students=StudentRepo(client),
        opportunities=OpportunityRepo(client),
        raw_emails=RawEmailRepo(client),
        drafts=DraftRepo(client),
        reminders=ReminderRepo(client),
    )
