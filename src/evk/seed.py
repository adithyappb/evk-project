"""Seed Firestore with demo students and opportunities.

Reads ./seed/students.json and ./seed/opportunities.json from the project root.
Idempotent: re-running updates existing docs in place.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from evk.factory import get_repos
from evk.firestore_repo import OpportunityRepo, Repos
from evk.logging import logger
from evk.models import Opportunity, Student

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SEED_DIR = PROJECT_ROOT / "seed"


def load_students(path: Path | None = None) -> list[Student]:
    payload = json.loads((path or SEED_DIR / "students.json").read_text(encoding="utf-8"))
    return [Student.model_validate(s) for s in payload]


def load_opportunities(path: Path | None = None) -> list[Opportunity]:
    payload = json.loads((path or SEED_DIR / "opportunities.json").read_text(encoding="utf-8"))
    return [_coerce_opportunity(entry) for entry in payload]


def _coerce_opportunity(entry: dict) -> Opportunity:
    deadline_raw = entry.get("deadline")
    deadline: datetime | None = None
    if deadline_raw:
        if "T" in deadline_raw:
            deadline = datetime.fromisoformat(deadline_raw.replace("Z", "+00:00")).astimezone(UTC)
        else:
            d = date.fromisoformat(deadline_raw)
            deadline = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=UTC)
    doc_id = OpportunityRepo.stable_id(
        title=entry["title"],
        deadline_iso=deadline_raw,
    )
    return Opportunity(
        id=doc_id,
        title=entry["title"],
        kind=entry["kind"],
        organization=entry["organization"],
        summary=entry["summary"],
        eligibility=entry.get("eligibility", ""),
        deadline=deadline,
        url=entry.get("url"),
        location=entry.get("location", ""),
        tags=entry.get("tags", []),
        fields_of_study=entry.get("fields_of_study", []),
        min_level=entry.get("min_level", "other"),
        source_raw_email_id=None,
        source_subject="[seed]",
        source_sender="seed@evk.local",
    )


def seed_all(repos: Repos | None = None) -> dict[str, int]:
    """Idempotent seed. Uses batch commits (≤ 500/batch) for Firestore parity."""
    repos = repos or get_repos()
    students = load_students()
    opps = load_opportunities()
    n_students = repos.students.upsert_many(students)
    n_opps = repos.opportunities.upsert_many(opps)
    logger.bind(students=n_students, opportunities=n_opps).info("seed.complete")
    return {"students": n_students, "opportunities": n_opps}


__all__ = ["load_opportunities", "load_students", "seed_all"]
