"""Mode-aware factory returns the right concrete clients."""

from __future__ import annotations

from evk.config import get_settings
from evk.factory import (
    describe_wiring,
    get_gemini,
    get_inkbox,
    get_repos,
    reset_all_caches,
)


def _refresh():
    get_settings.cache_clear()
    reset_all_caches()


def test_local_mode_returns_stubs(monkeypatch):
    monkeypatch.setenv("EVK_MODE", "local")
    monkeypatch.setenv("INKBOX_API_KEY", "ApiKey_local_stub")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "")
    _refresh()

    wiring = describe_wiring()
    assert wiring["mode"] == "local"
    assert wiring["repos"] == "local_json"
    assert wiring["inkbox"] == "stub"
    assert wiring["gemini"] == "stub"

    # Verify the concrete instances.
    from evk.stubs import StubGemini, StubInkbox

    assert isinstance(get_inkbox(), StubInkbox)
    assert isinstance(get_gemini(), StubGemini)


def test_local_mode_with_google_api_key_upgrades_gemini(monkeypatch):
    monkeypatch.setenv("EVK_MODE", "local")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-gemini-key")
    monkeypatch.setenv("INKBOX_API_KEY", "ApiKey_local_stub")
    _refresh()

    wiring = describe_wiring()
    assert wiring["gemini"] == "gemini_dev_api"


def test_local_mode_reads_real_inkbox_when_key_looks_real(monkeypatch):
    monkeypatch.setenv("EVK_MODE", "local")
    monkeypatch.setenv("INKBOX_API_KEY", "real_looking_key_abcdef")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "")
    _refresh()
    wiring = describe_wiring()
    assert wiring["inkbox"] == "real"


def test_repos_return_repos_object(monkeypatch, tmp_path):
    monkeypatch.setenv("EVK_MODE", "local")
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    _refresh()
    repos = get_repos()
    # Basic CRUD smoke test through the factory.
    assert repos.students.list_all() == []
