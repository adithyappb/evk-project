"""Mode-aware client factories. This is the ONLY module that knows whether we're
running in `local` or `production` mode. Everything else takes the returned
objects as opaque interfaces.

In local mode we return file-backed stores and stubbed Inkbox/Gemini so the
app works end-to-end with no credentials. In production we return the real
Firestore / Inkbox / Vertex AI clients.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from evk.config import Settings, get_settings
from evk.firestore_repo import Repos
from evk.logging import logger

# --------------------------------------------------------------------------- #
# Firestore / local store                                                     #
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def get_repos() -> Repos:
    settings = get_settings()
    if settings.is_local:
        from evk.local_store import build_local_repos

        logger.bind(mode="local", dir=settings.local_data_dir).info("repos.local")
        return build_local_repos()
    from evk.firestore_repo import get_repos as _prod_get_repos

    logger.bind(mode="production", project=settings.effective_firestore_project).info(
        "repos.firestore"
    )
    return _prod_get_repos()


# --------------------------------------------------------------------------- #
# Inkbox                                                                      #
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def get_inkbox() -> Any:
    settings = get_settings()
    if settings.is_local and _looks_like_stub_key(settings.inkbox_api_key):
        from evk.stubs import StubInkbox

        logger.bind(mode="local").info("inkbox.stub")
        return StubInkbox()
    from evk.inkbox_client import InkboxClient

    logger.info("inkbox.real")
    return InkboxClient()


def _looks_like_stub_key(key: str) -> bool:
    return (not key) or key.startswith(("ApiKey_local", "ApiKey_replace", "ApiKey_test"))


# --------------------------------------------------------------------------- #
# Gemini                                                                      #
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def get_gemini() -> Any:
    settings = get_settings()
    if settings.is_local and not settings.google_api_key:
        from evk.stubs import StubGemini

        logger.info("gemini.stub")
        return StubGemini()
    from evk.gemini_client import GeminiClient

    return GeminiClient()


# --------------------------------------------------------------------------- #
# Diagnostic helpers                                                          #
# --------------------------------------------------------------------------- #


def describe_wiring(settings: Settings | None = None) -> dict[str, str]:
    """Return a human-readable summary of which backends are wired up."""
    s = settings or get_settings()
    return {
        "mode": s.evk_mode,
        "repos": "local_json" if s.is_local else "firestore",
        "inkbox": "stub" if (s.is_local and _looks_like_stub_key(s.inkbox_api_key)) else "real",
        "gemini": (
            "stub"
            if (s.is_local and not s.google_api_key)
            else ("gemini_dev_api" if s.google_api_key else "vertex_ai")
        ),
        "data_dir": s.local_data_dir if s.is_local else "(n/a)",
    }


def reset_all_caches() -> None:
    """Clear factory caches (useful in tests when settings change)."""
    get_repos.cache_clear()
    get_inkbox.cache_clear()
    get_gemini.cache_clear()


__all__ = [
    "describe_wiring",
    "get_gemini",
    "get_inkbox",
    "get_repos",
    "reset_all_caches",
]
