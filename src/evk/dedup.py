"""Opportunity deduplication — cheap pre-filter + Jaccard + cosine similarity.

Strategy (per production mandate):

1. **Exact** — the stable ID already collapses ``(title, deadline)`` duplicates
   at write time. That catches ~all repeats across the same newsletter vendor.
2. **Pre-filter** — when considering a new candidate, only compare against
   existing opportunities with the same ``kind`` **and** a deadline within
   ``dedup_deadline_window_days`` (default 30 d). This prunes an O(N^2) space
   down to O(N x k) where k ~= a handful.
3. **Fuzzy** — within the candidate set, compute Jaccard similarity on the
   title's alphanumeric token set. ≥ 0.7 is treated as a near-duplicate.
4. **Semantic** — when both candidate and existing have embeddings stored,
   also check cosine similarity ≥ 0.92 to catch near-duplicates with
   different titles (e.g. same event from two newsletters).
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
        # Semantic check: cosine similarity on embeddings when available.
        cos = _cosine(candidate.embedding, other.embedding)
        if cos >= 0.92 and (best is None or cos > best.similarity):
            best = DuplicateMatch(existing=other, similarity=cos)
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


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two embedding vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


__all__ = ["DuplicateMatch", "find_duplicate"]
