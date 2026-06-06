"""Unit tests for ``evk.dedup.find_duplicate`` — the fuzzy pre-filter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evk.dedup import find_duplicate
from evk.models import Opportunity, OpportunityKind, StudentLevel


def _opp(
    *,
    id: str,
    title: str,
    kind: OpportunityKind = OpportunityKind.INTERNSHIP,
    deadline: datetime | None = None,
) -> Opportunity:
    return Opportunity(
        id=id,
        title=title,
        kind=kind,
        organization="Acme",
        summary="Summary",
        eligibility="any",
        deadline=deadline,
        url=None,
        location="Remote",
        tags=[],
        fields_of_study=[],
        min_level=StudentLevel.UNDERGRAD,
        source_raw_email_id="",
        source_subject="",
        source_sender="",
    )


def test_no_duplicates_when_catalogue_empty():
    c = _opp(id="c", title="Google Summer of Code 2026", deadline=datetime(2026, 3, 18, tzinfo=UTC))
    assert find_duplicate(c, []) is None


def test_prefilters_by_kind_even_when_titles_identical():
    now = datetime(2026, 3, 1, tzinfo=UTC)
    a = _opp(
        id="a", title="Applied Research Program 2026", kind=OpportunityKind.INTERNSHIP, deadline=now
    )
    b = _opp(
        id="b",
        title="Applied Research Program 2026",
        kind=OpportunityKind.SCHOLARSHIP,  # different kind!
        deadline=now,
    )
    assert find_duplicate(a, [b]) is None


def test_prefilters_by_deadline_window():
    a = _opp(id="a", title="Mozilla Tech Fund 2026", deadline=datetime(2026, 4, 30, tzinfo=UTC))
    # b has same title but deadline is 6 months away — well outside the default 30d window
    b = _opp(id="b", title="Mozilla Tech Fund 2026", deadline=datetime(2026, 10, 1, tzinfo=UTC))
    assert find_duplicate(a, [b], window_days=30) is None


def test_finds_obvious_near_duplicate_within_window():
    deadline = datetime(2026, 3, 18, tzinfo=UTC)
    existing = _opp(id="existing", title="Google Summer of Code 2026 (GSoC)", deadline=deadline)
    candidate = _opp(
        id="candidate",
        title="Google Summer of Code 2026",
        deadline=deadline + timedelta(days=5),
    )
    match = find_duplicate(candidate, [existing])
    assert match is not None
    assert match.existing.id == "existing"
    assert match.similarity >= 0.7


def test_rolling_deadline_only_matches_other_rolling():
    a = _opp(id="a", title="Open Source Apprenticeship", deadline=None)
    b = _opp(
        id="b",
        title="Open Source Apprenticeship",
        deadline=datetime(2026, 6, 1, tzinfo=UTC),
    )
    assert find_duplicate(a, [b]) is None

    c = _opp(id="c", title="Open Source Apprenticeship", deadline=None)
    match = find_duplicate(a, [c])
    assert match is not None


def test_ignores_itself():
    deadline = datetime(2026, 3, 1, tzinfo=UTC)
    opp = _opp(id="same", title="Rhodes Scholarship 2026", deadline=deadline)
    assert find_duplicate(opp, [opp]) is None


def test_requires_real_token_overlap():
    deadline = datetime(2026, 3, 1, tzinfo=UTC)
    a = _opp(id="a", title="Rhodes Scholarship 2026", deadline=deadline)
    b = _opp(id="b", title="Fulbright Grant Program", deadline=deadline)
    assert find_duplicate(a, [b]) is None
