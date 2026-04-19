"""Privacy layer — pseudonymise student PII before any Gemini call.

Rules (non-negotiable, per production mandate):

* **Identifiers** → irreversible SHA-256 hash (salted), truncated to 16 hex.
* **Age** → coarse band: ``14-16`` / ``17-18`` / ``19+``. Graduation year is
  converted to an age band on the fly when DOB / age is absent.
* **Location** → region only: we keep the last country/region token of the
  student's location string and drop street / city / ZIP. Unknown → ``"—"``.
* **Name** → first name only (so the copy can still greet them warmly).
* **Email** → **never** shipped to Gemini; it stays in the draft envelope only.

The output is a plain ``dict`` safe to embed in any Gemini prompt.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from evk.config import get_settings
from evk.models import Student

_AGE_BANDS: Final[tuple[tuple[int, int, str], ...]] = (
    (0, 16, "14-16"),
    (17, 18, "17-18"),
    (19, 200, "19+"),
)

# Country / region tokens we recognise at the *tail* of a location string.
# The logic is conservative: last comma-separated non-empty chunk is the region.
_DEFAULT_REGION = "—"


@dataclass(frozen=True, slots=True)
class PseudonymisedStudent:
    """Minimal, anonymised view of a student safe for LLM prompts."""

    sid: str  # salted hash, 16 hex
    first_name: str
    level: str
    age_band: str
    region: str
    fields_of_study: tuple[str, ...]
    interests: tuple[str, ...]
    bio: str

    def to_prompt_block(self) -> str:
        """Stable, human-readable serialisation for inclusion in prompts."""
        fields = ", ".join(self.fields_of_study) or "n/a"
        interests = ", ".join(self.interests) or "n/a"
        return (
            "STUDENT (pseudonymised)\n"
            f"- Id: {self.sid}\n"
            f"- First name: {self.first_name}\n"
            f"- Level: {self.level}\n"
            f"- Age band: {self.age_band}\n"
            f"- Region: {self.region}\n"
            f"- Fields: {fields}\n"
            f"- Interests: {interests}\n"
            f"- Bio: {self.bio or 'n/a'}"
        )


# --------------------------------------------------------------------------- #
# Core pseudonymisation                                                       #
# --------------------------------------------------------------------------- #


def pseudonymise(
    student: Student, *, salt: str | None = None, now: datetime | None = None
) -> PseudonymisedStudent:
    """Return a ``PseudonymisedStudent`` safe to include in a Gemini prompt."""
    effective_salt = salt if salt is not None else get_settings().privacy_salt
    first_name = (student.name or "").strip().split(" ", 1)[0] or "friend"
    return PseudonymisedStudent(
        sid=_hash_id(student.id, salt=effective_salt),
        first_name=first_name,
        level=student.level.value,
        age_band=_age_band(student, now=now or datetime.now(UTC)),
        region=_region_only(student.location),
        fields_of_study=tuple(f.lower().strip() for f in student.fields_of_study if f.strip()),
        interests=tuple(i.lower().strip() for i in student.interests if i.strip()),
        bio=(student.bio or "").strip()[:400],
    )


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _hash_id(raw_id: str, *, salt: str) -> str:
    h = hashlib.sha256(f"{salt}|{raw_id}".encode())
    return h.hexdigest()[:16]


def _age_band(student: Student, *, now: datetime) -> str:
    """Map graduation year → plausible age band.

    Heuristic: assume high_school graduates at 18, undergrad at 22, grad at 26.
    If no ``graduation_year`` is known, fall back to the student's level.
    """
    if student.graduation_year:
        assumed_age_at_grad = {
            "high_school": 18,
            "undergrad": 22,
            "grad": 26,
            "other": 20,
        }[student.level.value]
        years_to_grad = student.graduation_year - now.year
        age = assumed_age_at_grad - max(years_to_grad, 0)
        return _band_for_age(age)
    return {
        "high_school": "14-16",
        "undergrad": "19+",
        "grad": "19+",
        "other": "17-18",
    }[student.level.value]


def _band_for_age(age: int) -> str:
    for low, high, label in _AGE_BANDS:
        if low <= age <= high:
            return label
    return "19+"


def _region_only(location: str) -> str:
    """Keep only the last comma-separated chunk — country / region."""
    if not location:
        return _DEFAULT_REGION
    tokens = [t.strip() for t in location.split(",") if t.strip()]
    if not tokens:
        return _DEFAULT_REGION
    # Take the final token and clip it to avoid shipping a full address.
    return tokens[-1][:40]


__all__ = ["PseudonymisedStudent", "pseudonymise"]
