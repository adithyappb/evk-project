"""Shared UI helpers — recommendations, deadlines, flash redirects."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse

from evk.agents.personalizer import score_match
from evk.firestore_repo import Repos
from evk.models import Opportunity, Student

_RESEND_LOCK = threading.Lock()
_RESEND_HITS: dict[str, list[float]] = {}
_RESEND_WINDOW_SEC = 300.0
_RESEND_MAX = 5


def allow_auth_resend(email: str) -> bool:
    """Return False when an email has exceeded OTP resend attempts in the window."""
    now = time.monotonic()
    key = email.strip().lower()
    with _RESEND_LOCK:
        hits = [t for t in _RESEND_HITS.get(key, []) if now - t < _RESEND_WINDOW_SEC]
        if len(hits) >= _RESEND_MAX:
            _RESEND_HITS[key] = hits
            return False
        hits.append(now)
        _RESEND_HITS[key] = hits
        return True

def parse_deadline_form(deadline_str: str) -> datetime | None:
    """Parse HTML date input; return None if blank or invalid."""
    raw = deadline_str.strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=UTC)
    except ValueError:
        return None


def recommend_for_student(
    student: Student | None,
    repos: Repos,
    decorate,
    *,
    limit: int = 12,
    min_score: float = 0.35,
) -> list:
    """Return ``OpportunityView`` rows scored and sorted for one student."""
    catalog = [
        o
        for o in repos.opportunities.list_all(limit=200)
        if not o.needs_review and not o.is_duplicate
    ]
    if student is None:
        return decorate(catalog[:limit])

    scored: list[tuple[float, list[str], Opportunity]] = []
    for opp in catalog:
        match = score_match(student, opp)
        if match.score >= min_score:
            scored.append((match.score, match.reasons, opp))
    scored.sort(key=lambda row: row[0], reverse=True)
    views = decorate([row[2] for row in scored[:limit]])
    for view, row in zip(views, scored[:limit], strict=True):
        view.match_score = row[0]
        view.match_reasons = row[1]
    return views


def flash_redirect(request: Request, route_name: str, message: str, **params: str) -> RedirectResponse:
    """303 redirect with a URL-encoded flash query param."""
    url = request.url_for(route_name, **params)
    return RedirectResponse(f"{url}?flash={quote(message)}", status_code=303)
