"""Unit tests for ``evk.privacy.pseudonymise``."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from evk.models import Student, StudentLevel
from evk.privacy import pseudonymise


def _make_student(**overrides) -> Student:
    base = dict(
        id="student_aanya_patel",
        name="Aanya Patel",
        email="aanya.patel@example.edu",
        level=StudentLevel.UNDERGRAD,
        school="IIT Bombay",
        graduation_year=2027,
        fields_of_study=["Computer Science", "AI"],
        interests=["Robotics"],
        location="Mumbai, India",
        bio="Aspiring researcher.",
        opted_in=True,
    )
    base.update(overrides)
    return Student(**base)


def test_pseudonymise_masks_identifiers_but_keeps_first_name():
    ps = pseudonymise(_make_student(), salt="unit-test")
    assert ps.first_name == "Aanya"
    assert ps.sid != "student_aanya_patel"
    assert len(ps.sid) == 16
    # full name never appears in the prompt block
    assert "Patel" not in ps.to_prompt_block()


def test_pseudonymise_strips_raw_email_and_street():
    s = _make_student(location="221B Baker Street, London, UK")
    ps = pseudonymise(s, salt="unit-test")
    block = ps.to_prompt_block()
    # email stays in the draft envelope; it must NEVER reach Gemini
    assert "aanya.patel@example.edu" not in block
    # only the last region token survives
    assert "Baker" not in block
    assert ps.region == "UK"


def test_age_band_derived_from_graduation_year():
    # Graduating in 2027 as an undergrad → assumed age 22-2=20 → band "19+"
    ps = pseudonymise(
        _make_student(graduation_year=2027),
        salt="t",
        now=datetime(2025, 1, 1, tzinfo=UTC),
    )
    assert ps.age_band == "19+"

    # High school, graduation 2026 → assumed age 18-1=17 → "17-18"
    ps = pseudonymise(
        _make_student(level=StudentLevel.HIGH_SCHOOL, graduation_year=2026),
        salt="t",
        now=datetime(2025, 1, 1, tzinfo=UTC),
    )
    assert ps.age_band == "17-18"


def test_age_band_fallback_without_graduation_year():
    ps = pseudonymise(
        _make_student(level=StudentLevel.HIGH_SCHOOL, graduation_year=None),
        salt="t",
    )
    assert ps.age_band == "14-16"


def test_hash_is_deterministic_and_salt_sensitive():
    a = pseudonymise(_make_student(), salt="salt-A")
    b = pseudonymise(_make_student(), salt="salt-A")
    c = pseudonymise(_make_student(), salt="salt-B")
    assert a.sid == b.sid
    assert a.sid != c.sid


@pytest.mark.parametrize(
    "location,expected",
    [
        ("", "—"),
        ("Remote", "Remote"),
        ("Mumbai, India", "India"),
        (", , ,", "—"),
        ("A Very Long Location " * 10, ("A Very Long Location " * 10)[:40]),
    ],
)
def test_region_only(location: str, expected: str):
    ps = pseudonymise(_make_student(location=location), salt="t")
    assert ps.region == expected
