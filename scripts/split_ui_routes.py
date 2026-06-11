"""One-shot splitter: routes.py -> routes/{auth,student,admin}.py + view_models.py"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTES = ROOT / "src" / "evk" / "ui" / "routes.py"
OUT = ROOT / "src" / "evk" / "ui" / "routes"

STUDENT_FUNCS = {
    "student_dashboard",
    "student_opportunity_detail",
    "student_outcome_save",
    "student_profile_page",
    "student_profile_save",
    "opportunity_suggest_page",
    "opportunity_suggest_submit",
}
AUTH_FUNCS = {
    "landing",
    "login_page",
    "register_page",
    "auth_signup",
    "auth_login",
    "auth_verify",
    "auth_resend",
    "forgot_page",
    "auth_forgot",
    "auth_reset",
    "logout",
    "profile_setup_page",
    "profile_setup_submit",
}

VIEW_MODELS_START = 67  # STATUS_TABS
VIEW_MODELS_END = 459   # before @router landing

MODULE_HEADER = '''"""{doc}"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from evk.agents.digest import DigestAgent
from evk.agents.distributor import DistributorAgent
from evk.agents.ingestion import IngestionAgent
from evk.agents.reminder import ReminderAgent
from evk.auth import AuthError, AuthService, TerminalAuthNotifier, build_auth_notifier
from evk.config import Settings, get_settings
from evk.factory import describe_wiring, get_inkbox, get_repos
from evk.firestore_repo import Repos
from evk.inkbox_client import InboundMessage, InkboxClient
from evk.logging import logger
from evk.models import AppUser, DraftMessage, DraftStatus, LoginChallenge, Opportunity, Student, StudentLevel, UserRole
from evk.ui.csrf import require_csrf
from evk.ui.deps import (
    auth_dep,
    current_user,
    distributor_dep,
    ingestion_dep,
    redirect,
    reminder_dep,
    repos_dep,
    settings_dep,
    set_session_cookie,
    staff_required,
)
from evk.ui.helpers import allow_auth_resend, flash_redirect, parse_deadline_form, recommend_for_student
from evk.ui.template_env import templates
from evk.ui.view_models import (
    STATUS_TABS,
    _STATUS_LITERAL,
    auth_page_context,
    dashboard_context,
    decorate_opps,
    draft_panel_context,
    render_drafts_panel,
    render_drafts_with_stats,
    render_stats,
    render_user_panel,
    role_home,
    staff_or_ngo_redirect,
    staff_redirect,
    stats as _stats,
)

router = APIRouter(tags=["ui-{tag}"])

'''


def extract_route_chunks(lines: list[str]) -> dict[str, list[str]]:
    first = next(i for i, l in enumerate(lines) if l.startswith("@router."))
    chunks: dict[str, list[str]] = {}
    i = first
    while i < len(lines):
        if lines[i].startswith("@router."):
            name = None
            for j in range(i + 1, min(i + 6, len(lines))):
                m = re.match(r"^def (\w+)\(", lines[j])
                if m:
                    name = m.group(1)
                    break
            start = i
            i += 1
            while i < len(lines) and not lines[i].startswith("@router."):
                i += 1
            chunks[name or str(start)] = lines[start:i]
        else:
            i += 1
    return chunks


def rewrite_route_block(block: list[str]) -> list[str]:
    """Prefix private helpers with view_models/deps imports."""
    text = "\n".join(block)
    replacements = [
        ("Depends(_repos_dep)", "Depends(repos_dep)"),
        ("Depends(_inkbox_dep)", "Depends(get_inkbox)"),
        ("Depends(_settings_dep)", "Depends(settings_dep)"),
        ("Depends(_auth_dep)", "Depends(auth_dep)"),
        ("Depends(_ingestion_dep)", "Depends(ingestion_dep)"),
        ("Depends(_distributor_dep)", "Depends(distributor_dep)"),
        ("Depends(_reminder_dep)", "Depends(reminder_dep)"),
        ("Depends(_current_user)", "Depends(current_user)"),
        ("_redirect(", "redirect("),
        ("_set_session_cookie(", "set_session_cookie("),
        ("_clear_session(", "clear_session("),
        ("_staff_required(", "staff_required("),
        ("_staff_redirect(", "staff_redirect("),
        ("_staff_or_ngo_redirect(", "staff_or_ngo_redirect("),
        ("_role_home(", "role_home("),
        ("_auth_page_context(", "auth_page_context("),
        ("_dashboard_context(", "dashboard_context("),
        ("_draft_panel_context(", "draft_panel_context("),
        ("_render_drafts_panel(", "render_drafts_panel("),
        ("_render_drafts_with_stats(", "render_drafts_with_stats("),
        ("_render_stats(", "render_stats("),
        ("_render_user_panel(", "render_user_panel("),
        ("_decorate_opps", "decorate_opps"),
        ("_stats(", "_stats("),
        ("_STATUS_LITERAL", "_STATUS_LITERAL"),
        ("STATUS_TABS", "STATUS_TABS"),
    ]
    for old, new in replacements:
        text = text.replace(old, new)
    # POST routes: inject require_csrf dependency
    if "@router.post" in text and "Depends(require_csrf)" not in text:
        text = re.sub(
            r"(def \w+\(\n\s+request: Request,\n)",
            r"\1    _: None = Depends(require_csrf),\n",
            text,
            count=1,
        )
    return text.splitlines()


def main() -> None:
    lines = ROUTES.read_text(encoding="utf-8").splitlines()
    vm_lines = lines[VIEW_MODELS_START - 1 : VIEW_MODELS_END]
    vm_body = "\n".join(vm_lines)
    # Fix names in view_models
    vm_body = vm_body.replace("def _humandate", "def humandate")
    vm_body = vm_body.replace("def _humandatetime", "def humandatetime")
    vm_body = vm_body.replace("def _level_label", "def level_label")
    vm_body = vm_body.replace("def _decorate_opps", "def decorate_opps")
    vm_body = vm_body.replace("def _stats(", "def stats(")
    vm_body = vm_body.replace("def _status_counts", "def status_counts")
    vm_body = vm_body.replace("def _role_summary", "def role_summary")
    vm_body = vm_body.replace("def _setup_status", "def setup_status")
    vm_body = vm_body.replace("def _dashboard_context", "def dashboard_context")
    vm_body = vm_body.replace("def _render_drafts(", "def render_drafts(")
    vm_body = vm_body.replace("def _draft_panel_context", "def draft_panel_context")
    vm_body = vm_body.replace("def _render_drafts_panel", "def render_drafts_panel")
    vm_body = vm_body.replace("def _render_stats", "def render_stats")
    vm_body = vm_body.replace("def _render_user_panel", "def render_user_panel")
    vm_body = vm_body.replace("def _render_drafts_with_stats", "def render_drafts_with_stats")
    vm_body = vm_body.replace("def _staff_redirect", "def staff_redirect")
    vm_body = vm_body.replace("def _staff_or_ngo_redirect", "def staff_or_ngo_redirect")
    vm_body = vm_body.replace("def _role_home", "def role_home")
    vm_body = vm_body.replace("def _auth_page_context", "def auth_page_context")
    vm_body = vm_body.replace("def _render_fragment", "def render_fragment")
    vm_body = vm_body.replace("_render_drafts(", "render_drafts(")
    vm_body = vm_body.replace("_draft_panel_context(", "draft_panel_context(")
    vm_body = vm_body.replace("_stats(repos)", "stats(repos)")
    vm_body = vm_body.replace("_status_counts(repos)", "status_counts(repos)")
    vm_body = vm_body.replace("_role_summary(repos)", "role_summary(repos)")
    vm_body = vm_body.replace("_setup_status(repos", "setup_status(repos")
    vm_body = vm_body.replace("_decorate_opps(", "decorate_opps(")

    vm_file = f'''"""Shared view models and render helpers for UI routes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from evk.config import Settings, get_settings
from evk.factory import describe_wiring
from evk.firestore_repo import Repos
from evk.models import AppUser, DraftMessage, DraftStatus, Opportunity, StudentLevel, UserRole
from evk.ui.deps import redirect
from evk.ui.template_env import templates

{vm_body}

# Public aliases
_redirect = redirect
'''
    (ROOT / "src" / "evk" / "ui" / "view_models.py").write_text(vm_file, encoding="utf-8")

    chunks = extract_route_chunks(lines)
    buckets: dict[str, list[str]] = {"auth": [], "student": [], "admin": []}
    for name, block in chunks.items():
        if name in AUTH_FUNCS:
            buckets["auth"].extend(rewrite_route_block(block))
            buckets["auth"].append("")
        elif name in STUDENT_FUNCS:
            buckets["student"].extend(rewrite_route_block(block))
            buckets["student"].append("")
        else:
            buckets["admin"].extend(rewrite_route_block(block))
            buckets["admin"].append("")

    OUT.mkdir(exist_ok=True)
    docs = {
        "auth": "Authentication, registration, password reset, profile setup.",
        "student": "Student dashboard, profile, suggestions, outcomes.",
        "admin": "Admin/NGO dashboards, drafts, opportunities, HTMX fragments.",
    }
    for tag, body in buckets.items():
        content = MODULE_HEADER.format(doc=docs[tag], tag=tag) + "\n".join(body)
        # fix clear_session import for auth logout
        if tag == "auth":
            content = content.replace(
                "from evk.ui.deps import (",
                "from evk.ui.deps import (\n    clear_session,\n",
            )
        (OUT / f"{tag}.py").write_text(content, encoding="utf-8")

    init = '''"""Combined UI router."""

from __future__ import annotations

from fastapi import APIRouter

from evk.ui.routes.admin import router as admin_router
from evk.ui.routes.auth import router as auth_router
from evk.ui.routes.student import router as student_router

router = APIRouter(tags=["ui"])
router.include_router(auth_router)
router.include_router(student_router)
router.include_router(admin_router)

__all__ = ["router"]
'''
    (OUT / "__init__.py").write_text(init, encoding="utf-8")
    print("Wrote view_models.py and routes/{auth,student,admin}.py")


if __name__ == "__main__":
    main()
