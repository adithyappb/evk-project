"""Tests for the rate-limit / daily-quota helper."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from evk import ratelimit
from evk.ratelimit import BATCH_HARD_CAP, DailyQuota, QuotaExceededError, batched


def test_batched_respects_hard_cap():
    items = list(range(100))
    out = batched(items, size=1000)
    # Batches are capped at BATCH_HARD_CAP (45), regardless of size arg.
    assert all(len(b) <= BATCH_HARD_CAP for b in out)
    assert sum(len(b) for b in out) == 100


def test_batched_small_size_works():
    out = batched([1, 2, 3, 4, 5], size=2)
    assert out == [[1, 2], [3, 4], [5]]


def test_batched_empty_list():
    assert batched([], size=10) == []


def test_daily_quota_charges_and_tracks():
    q = DailyQuota(limit=5)
    q.charge(2)
    q.charge(2)
    snap = q.snapshot()
    assert snap["used"] == 4
    assert snap["remaining"] == 1


def test_daily_quota_raises_at_limit():
    q = DailyQuota(limit=3)
    q.charge(3)
    with pytest.raises(QuotaExceededError):
        q.charge(1)


def test_daily_quota_resets_on_new_day():
    q = DailyQuota(limit=2)
    q.charge(2)
    # Simulate midnight crossover — mutate the slot's day and charge again.
    q._slot.day = date.today() - timedelta(days=1)  # type: ignore[attr-defined]
    q.charge(1)
    assert q.snapshot()["used"] == 1


def test_sleep_between_respects_zero(monkeypatch):
    # With 0s configured, sleep_between must not actually sleep.
    called: list[float] = []

    def fake_sleep(s: float) -> None:
        called.append(s)

    monkeypatch.setattr(ratelimit.time, "sleep", fake_sleep)
    ratelimit.sleep_between(0)
    assert called == []


def test_sleep_between_calls_time_sleep(monkeypatch):
    called: list[float] = []

    def fake_sleep(s: float) -> None:
        called.append(s)

    monkeypatch.setattr(ratelimit.time, "sleep", fake_sleep)
    ratelimit.sleep_between(0.05)
    assert called == [0.05]
