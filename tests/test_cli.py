from __future__ import annotations

import pytest
import typer

from evk.cli import _resolve_bind_port


def test_resolve_bind_port_falls_forward_when_default_port_is_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_port_is_available(host: str, port: int) -> bool:
        return port != 8080

    monkeypatch.setattr("evk.cli._port_is_available", fake_port_is_available)

    assert _resolve_bind_port("0.0.0.0", 8080, allow_fallback=True) == 8081


def test_resolve_bind_port_stays_strict_for_explicit_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("evk.cli._port_is_available", lambda host, port: False)

    with pytest.raises(typer.BadParameter, match=r"Port 8080 is already in use"):
        _resolve_bind_port("0.0.0.0", 8080, allow_fallback=False)
