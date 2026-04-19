"""File-backed local replacements for Firestore repositories.

Each collection is stored as a JSON file under the configured data dir.
Writes are atomic (write-temp-then-rename). All methods mirror the real
``firestore_repo`` public surface so they're drop-in substitutes.

This module exists purely so EVK boots and runs with no real credentials —
the production path still uses ``firestore_repo.get_repos()``.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Generic, TypeVar

from evk.config import get_settings
from evk.firestore_repo import (
    DraftRepo,
    OpportunityRepo,
    RawEmailRepo,
    ReminderRepo,
    Repos,
    StudentRepo,
)
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

_LOCK = threading.RLock()


# --------------------------------------------------------------------------- #
# JSON collection helper                                                      #
# --------------------------------------------------------------------------- #


class _JsonCollection(Generic[T]):
    """A single on-disk JSON file keyed by doc id, atomic writes."""

    def __init__(self, path: Path, model: type[T]) -> None:
        self._path = path
        self._model = model
        self._cache: dict[str, T] | None = None
        self._cache_mtime: float = -1.0

    def _load(self) -> dict[str, T]:
        # Refresh cache when the file on disk has changed — in local dev,
        # multiple processes may be writing concurrently (API + CLI + tests).
        if not self._path.exists():
            if self._cache is None or self._cache_mtime != 0.0:
                self._cache = {}
                self._cache_mtime = 0.0
            return self._cache
        mtime = self._path.stat().st_mtime
        if self._cache is not None and mtime == self._cache_mtime:
            return self._cache
        raw = json.loads(self._path.read_text(encoding="utf-8") or "{}")
        self._cache = {
            doc_id: self._model.model_validate({"id": doc_id, **data})
            for doc_id, data in raw.items()
        }
        self._cache_mtime = mtime
        return self._cache

    def _save(self) -> None:
        assert self._cache is not None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            doc.id: doc.model_dump(mode="json", exclude={"id"}) for doc in self._cache.values()
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        # On Windows, antivirus / indexing can briefly lock the target file.
        last_exc: Exception | None = None
        for attempt in range(5):
            try:
                os.replace(tmp, self._path)
                self._cache_mtime = self._path.stat().st_mtime
                return
            except PermissionError as exc:
                last_exc = exc
                time.sleep(0.05 * (attempt + 1))
        raise last_exc if last_exc else RuntimeError("atomic rename failed")

    # CRUD ------------------------------------------------------------------

    def get(self, doc_id: str) -> T | None:
        with _LOCK:
            return self._clone(self._load().get(doc_id))

    def upsert(self, doc: T) -> T:
        with _LOCK:
            data = self._load()
            data[doc.id] = self._clone(doc)
            self._save()
            return doc

    def upsert_many(self, docs: list[T], *, batch_size: int = 500) -> int:
        """Bulk upsert mirroring Firestore's 500-op batch semantics."""
        if not docs:
            return 0
        size = max(1, min(batch_size, 500))
        written = 0
        with _LOCK:
            data = self._load()
            for i, doc in enumerate(docs, 1):
                data[doc.id] = self._clone(doc)
                written += 1
                if i % size == 0:
                    self._save()
            self._save()
        return written

    def create_if_absent(self, doc: T) -> tuple[T, bool]:
        with _LOCK:
            data = self._load()
            if doc.id in data:
                return self._clone(data[doc.id]), False
            data[doc.id] = self._clone(doc)
            self._save()
            return doc, True

    def patch(self, doc_id: str, fields: dict[str, Any]) -> None:
        with _LOCK:
            data = self._load()
            if doc_id not in data:
                raise KeyError(doc_id)
            current = data[doc_id].model_dump()
            current.update(fields)
            data[doc_id] = self._model.model_validate(current)
            self._save()

    def delete(self, doc_id: str) -> None:
        with _LOCK:
            data = self._load()
            if doc_id in data:
                del data[doc_id]
                self._save()

    def list_all(self, limit: int | None = None) -> list[T]:
        with _LOCK:
            items = [self._clone(v) for v in self._load().values()]
            return items[:limit] if limit else items

    @staticmethod
    def _clone(obj: T | None) -> T | None:
        if obj is None:
            return None
        return obj.__class__.model_validate(obj.model_dump())


# --------------------------------------------------------------------------- #
# Base mixin — forwards standard CRUD to ``self._col`` so each concrete       #
# local repo only needs to declare its specialised queries.                   #
# --------------------------------------------------------------------------- #


class _LocalRepoBase(Generic[T]):
    _col: _JsonCollection[T]

    def get(self, doc_id: str) -> T | None:
        return self._col.get(doc_id)

    def upsert(self, doc: T) -> T:
        return self._col.upsert(doc)

    def upsert_many(self, docs: list[T], *, batch_size: int = 500) -> int:
        return self._col.upsert_many(docs, batch_size=batch_size)

    def create_if_absent(self, doc: T) -> tuple[T, bool]:
        return self._col.create_if_absent(doc)

    def patch(self, doc_id: str, fields: dict[str, Any]) -> None:
        self._col.patch(doc_id, fields)

    def delete(self, doc_id: str) -> None:
        self._col.delete(doc_id)

    def list_all(self, limit: int | None = None) -> list[T]:
        return self._col.list_all(limit)


# --------------------------------------------------------------------------- #
# Concrete repos — subclass real repos for isinstance/typing, mix in CRUD.   #
# --------------------------------------------------------------------------- #


class LocalStudentRepo(_LocalRepoBase[Student], StudentRepo):
    def __init__(self, path: Path) -> None:
        self._col = _JsonCollection(path, Student)

    def list_opted_in(self) -> list[Student]:
        return [s for s in self._col.list_all() if s.opted_in]


class LocalOpportunityRepo(_LocalRepoBase[Opportunity], OpportunityRepo):
    def __init__(self, path: Path) -> None:
        self._col = _JsonCollection(path, Opportunity)

    def with_upcoming_deadlines(self, *, within_days: int) -> list[Opportunity]:
        now = datetime.now(UTC)
        cutoff = now.timestamp() + within_days * 86_400
        upcoming = [
            o
            for o in self._col.list_all()
            if o.deadline and now.timestamp() <= o.deadline.timestamp() <= cutoff
        ]
        upcoming.sort(key=lambda o: o.deadline)  # type: ignore[arg-type, return-value]
        return upcoming


class LocalRawEmailRepo(_LocalRepoBase[RawEmail], RawEmailRepo):
    def __init__(self, path: Path) -> None:
        self._col = _JsonCollection(path, RawEmail)

    def exists(self, inkbox_message_id: str) -> bool:
        return self._col.get(inkbox_message_id) is not None

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
        self._col.patch(doc_id, patch)


class LocalDraftRepo(_LocalRepoBase[DraftMessage], DraftRepo):
    def __init__(self, path: Path) -> None:
        self._col = _JsonCollection(path, DraftMessage)

    def list_by_status(self, status: DraftStatus, limit: int = 200) -> list[DraftMessage]:
        items = [d for d in self._col.list_all() if d.status == status]
        items.sort(key=lambda d: d.created_at, reverse=True)
        return items[:limit]

    def exists_for_pair(self, student_id: str, opportunity_id: str) -> bool:
        return any(
            d.student_id == student_id and d.opportunity_id == opportunity_id
            for d in self._col.list_all()
        )


class LocalReminderRepo(_LocalRepoBase[ReminderLog], ReminderRepo):
    def __init__(self, path: Path) -> None:
        self._col = _JsonCollection(path, ReminderLog)

    def exists(self, student_id: str, opportunity_id: str, days_before: int) -> bool:
        return any(
            r.student_id == student_id
            and r.opportunity_id == opportunity_id
            and r.days_before == days_before
            for r in self._col.list_all()
        )


# --------------------------------------------------------------------------- #
# Factory                                                                     #
# --------------------------------------------------------------------------- #


def build_local_repos(data_dir: Path | None = None) -> Repos:
    settings = get_settings()
    root = Path(data_dir or settings.local_data_dir)
    root.mkdir(parents=True, exist_ok=True)
    return Repos(
        students=LocalStudentRepo(root / "students.json"),
        opportunities=LocalOpportunityRepo(root / "opportunities.json"),
        raw_emails=LocalRawEmailRepo(root / "raw_emails.json"),
        drafts=LocalDraftRepo(root / "drafts.json"),
        reminders=LocalReminderRepo(root / "reminders.json"),
    )


__all__ = ["build_local_repos"]
