"""Admin dashboard UI — Jinja2 templates, Tailwind (CDN), HTMX."""

from __future__ import annotations

from pathlib import Path

TEMPLATES_DIR = Path(__file__).parent / "templates"

__all__ = ["TEMPLATES_DIR"]
