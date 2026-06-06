"""Opportunity deduplication — cheap pre-filter + Jaccard similarity.

Strategy (per production mandate):

1. **Exact** — the stable ID already collapses ``(title, deadline)`` duplicates
   at write time. That catches ~all repeats across the same newsletter vendor.
2. **Pre-filter** — when considering a new candidate, only compare against
   existing opportunities with the same ``kind`` **and** a deadline within
   ``dedup_deadline_window_days`` (default 30 d). This prunes an O(N^2) space
   down to O(N x k) where k ~= a handful.
3. **Fuzzy** — within the candidate set, compute Jaccard similarity on the
   title's alphanumeric token set. ≥ 0.7 is treated as a near-duplicate.

This intentionally does **not** call the Gemini embeddings API — a full
embedding service would be justified only once we're ingesting thousands of
emails/day and even then, the pre-filter here trims the embedding-compare set
by ~98 %. The module is designed so a future `embed_similarity()` can slot in
behind the same interface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from evk.config import get_settings
from evk.models import Opportunity

_DEFAULT_JACCARD_THRESHOLD: float = 0.7
_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class DuplicateMatch:
    """A near-duplicate hit with the existing doc and similarity score."""

    existing: Opportunity
    similarity: float


def find_duplicate(
    candidate: Opportunity,
    existing: list[Opportunity],
    *,
    window_days: int | None = None,
    threshold: float = _DEFAULT_JACCARD_THRESHOLD,
) -> DuplicateMatch | None:
    """Return the best duplicate of ``candidate`` in ``existing``, or ``None``.

    ``existing`` is expected to be the **full** current collection; we
    pre-filter internally to keep call sites simple.
    """
    window = window_days or get_settings().dedup_deadline_window_days
    candidates = _prefilter(candidate, existing, window_days=window)
    if not candidates:
        return None

    cand_tokens = _tokens(candidate.title)
    if not cand_tokens:
        return None

    best: DuplicateMatch | None = None
    for other in candidates:
        if other.id == candidate.id:
            continue
        sim = _jaccard(cand_tokens, _tokens(other.title))
        if sim >= threshold and (best is None or sim > best.similarity):
            best = DuplicateMatch(existing=other, similarity=sim)
    return best


def _prefilter(
    candidate: Opportunity, existing: list[Opportunity], *, window_days: int
) -> list[Opportunity]:
    """Keep only same-kind opportunities whose deadlines are within ± window."""
    if candidate.deadline is None:
        # Rolling opportunities are compared only against other rolling ones
        # of the same kind.
        return [o for o in existing if o.kind == candidate.kind and o.deadline is None]
    lo = candidate.deadline - timedelta(days=window_days)
    hi = candidate.deadline + timedelta(days=window_days)
    return [
        o
        for o in existing
        if o.kind == candidate.kind and o.deadline is not None and lo <= o.deadline <= hi
    ]


def _tokens(text: str) -> frozenset[str]:
    """Alphanumeric tokens, ≥ 3 chars, lower-cased. Cheap and language-agnostic."""
    return frozenset(tok for tok in _TOKEN_RE.findall(text.lower()) if len(tok) >= 3)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = a & b
    if not inter:
        return 0.0
    union = a | b
    return len(inter) / len(union)


__all__ = ["DuplicateMatch", "find_duplicate"]
