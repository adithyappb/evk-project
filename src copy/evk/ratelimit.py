"""Send-side rate limiter + daily quota tracker.

Upstream mailbox providers (Gmail, Inkbox-over-SES, etc.) throttle both per-
request and per-day. The ``DailyQuota`` here tracks per-mailbox sends against
a soft cap and trips a circuit breaker at the cap; ``sleep_between`` spaces
successive sends by ``settings.delivery_delay_seconds``.

Batch commits are capped at ``settings.delivery_batch_size`` (default 45 — the
practical Gmail API batch recipient limit).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Final

from evk.config import get_settings
from evk.logging import logger

_LOCK = threading.Lock()


class QuotaExceededError(RuntimeError):
    """Raised when the daily send quota is exhausted for the current UTC day."""


@dataclass(slots=True)
class _QuotaSlot:
    day: date
    used: int = 0


@dataclass(slots=True)
class DailyQuota:
    """Thread-safe per-day send counter, bounded by ``limit``."""

    limit: int
    _slot: _QuotaSlot = field(default_factory=lambda: _QuotaSlot(day=_today()))

    def charge(self, n: int = 1) -> None:
        """Reserve ``n`` sends or raise ``QuotaExceededError``."""
        with _LOCK:
            today = _today()
            if self._slot.day != today:
                self._slot = _QuotaSlot(day=today)
            if self._slot.used + n > self.limit:
                raise QuotaExceededError(
                    f"daily send quota exhausted ({self._slot.used}/{self.limit})"
                )
            self._slot.used += n

    @property
    def used_today(self) -> int:
        return self._slot.used if self._slot.day == _today() else 0

    def snapshot(self) -> dict[str, int | str]:
        return {
            "day": self._slot.day.isoformat(),
            "used": self.used_today,
            "limit": self.limit,
            "remaining": max(0, self.limit - self.used_today),
        }


BATCH_HARD_CAP: Final[int] = 45  # Gmail API recipient cap per send call


def batched(items: list, size: int) -> list[list]:
    """Slice ``items`` into batches of ``size`` (each ≤ BATCH_HARD_CAP)."""
    size = min(max(1, size), BATCH_HARD_CAP)
    return [items[i : i + size] for i in range(0, len(items), size)]


def sleep_between(seconds: float | None = None) -> None:
    """Sleep between two consecutive sends (configurable, ≥ 0)."""
    s = seconds if seconds is not None else get_settings().delivery_delay_seconds
    if s > 0:
        time.sleep(s)


def _today() -> date:
    return datetime.now(UTC).date()


@lru_cache(maxsize=1)
def get_daily_quota() -> DailyQuota:
    """Process-wide daily quota singleton (sized from settings)."""
    limit = get_settings().delivery_daily_quota
    logger.bind(limit=limit).debug("ratelimit.quota_initialised")
    return DailyQuota(limit=limit)


__all__ = [
    "BATCH_HARD_CAP",
    "DailyQuota",
    "QuotaExceededError",
    "batched",
    "get_daily_quota",
    "sleep_between",
]
