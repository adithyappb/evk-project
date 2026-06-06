"""Newsletter sanitisation — strip boilerplate before classification.

Marketing footers, unsubscribe links, long signature blocks, and tracking
pixels are pure noise that waste Gemini tokens and can confuse the classifier.
This module removes them using conservative, reversible rules:

* Cut everything after a common unsubscribe separator (``-- ``, ``<hr>``,
  ``If you no longer wish…``, etc.).
* Drop ``View in browser`` / preheader scaffolding at the top.
* Collapse 3+ consecutive blank lines.
* Strip zero-width and tracking-pixel artefacts.
* Never shorten below a 1-sentence safety floor.

All rules are regex-level — no HTML parsing, no network.
"""

from __future__ import annotations

import re
from typing import Final

_FOOTER_MARKERS: Final[tuple[str, ...]] = (
    # Hard dividers
    "\n-- \n",
    "\n__",
    "\n==",
    # English boilerplate
    "unsubscribe",
    "manage your preferences",
    "manage preferences",
    "update your preferences",
    "you received this email because",
    "you are receiving this email because",
    "if you no longer wish",
    "view this email in your browser",
    "view in browser",
    "privacy policy",
    "© 20",
    "copyright 20",
)

_PREHEADER_NOISE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(view (this )?email in (your )?browser.*?\n)", re.IGNORECASE
)
_MULTI_BLANK: Final[re.Pattern[str]] = re.compile(r"\n{3,}")
_TRACKING_PIXEL: Final[re.Pattern[str]] = re.compile(
    r"<img[^>]*(height=[\"']?1[\"']?|width=[\"']?1[\"']?)[^>]*>", re.IGNORECASE
)
_ZERO_WIDTH: Final[re.Pattern[str]] = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")

# Keep at least this many characters so extremely-short messages aren't wiped.
_MIN_KEEP_CHARS: Final[int] = 40


def strip_footers(body: str) -> str:
    """Return the body with promo / unsubscribe / tracking boilerplate removed."""
    if not body:
        return body
    cleaned = _ZERO_WIDTH.sub("", body)
    cleaned = _TRACKING_PIXEL.sub("", cleaned)
    cleaned = _PREHEADER_NOISE.sub("", cleaned)

    # Find the earliest footer marker and truncate there.
    lower = cleaned.lower()
    cut_at = len(cleaned)
    for marker in _FOOTER_MARKERS:
        idx = lower.find(marker)
        if idx != -1 and idx < cut_at:
            cut_at = idx
    truncated = cleaned[:cut_at].rstrip()

    # Safety floor: if truncation murdered the body, keep the original.
    if len(truncated) < _MIN_KEEP_CHARS and len(cleaned) >= _MIN_KEEP_CHARS:
        truncated = cleaned

    truncated = _MULTI_BLANK.sub("\n\n", truncated)
    return truncated.strip()


__all__ = ["strip_footers"]
