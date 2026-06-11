"""Jinja2 environment, filters, and static asset helpers."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

from evk.config import get_settings
from evk.ui import TEMPLATES_DIR

STATIC_DIR = Path(__file__).parent / "static"
TAILWIND_CSS = STATIC_DIR / "css" / "app.css"


def humandate(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return f"{dt.strftime('%B')} {dt.day}, {dt.year}"


def humandatetime(dt: datetime | None) -> str:
    if dt is None:
        return ""
    hour = dt.hour % 12 or 12
    return f"{humandate(dt)} at {hour}:{dt.minute:02d} {dt.strftime('%p')}"


_LEVEL_LABELS = {
    "11th grade": "11th grade",
    "12th grade": "12th grade",
    "College year 1-4": "College (Years 1–4)",
    "Graduate": "Graduate student",
    "Alumni": "Alumni",
    "other": "Any / not specified",
}


def level_label(level: object) -> str:
    value = getattr(level, "value", level)
    return _LEVEL_LABELS.get(str(value), str(value).replace("_", " "))


def use_local_tailwind() -> bool:
    """Serve pinned Tailwind build in prod (or whenever the compiled file exists)."""
    settings = get_settings()
    if settings.app_env == "prod":
        return True
    return TAILWIND_CSS.is_file()


def tailwind_css_url() -> str:
    return "/static/css/app.css"


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["humandate"] = humandate
templates.env.filters["humandatetime"] = humandatetime
templates.env.filters["level_label"] = level_label
templates.env.globals["use_local_tailwind"] = use_local_tailwind
templates.env.globals["tailwind_css_url"] = tailwind_css_url

__all__ = [
    "STATIC_DIR",
    "TAILWIND_CSS",
    "humandate",
    "humandatetime",
    "level_label",
    "tailwind_css_url",
    "templates",
    "use_local_tailwind",
]
