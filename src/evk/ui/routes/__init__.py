"""Combined UI router and backward-compatible re-exports."""

from __future__ import annotations

from fastapi import APIRouter

from evk.ui.deps import (
    _auth_dep,
    _current_user,
    _distributor_dep,
    _ingestion_dep,
    _inkbox_dep,
    _repos_dep,
)
from evk.ui.routes.admin import router as admin_router
from evk.ui.routes.auth import router as auth_router
from evk.ui.routes.student import router as student_router
from evk.ui.view_models import _decorate_opps, decorate_opps

router = APIRouter(tags=["ui"])
router.include_router(auth_router)
router.include_router(student_router)
router.include_router(admin_router)

__all__ = [
    "_auth_dep",
    "_current_user",
    "_decorate_opps",
    "_distributor_dep",
    "_ingestion_dep",
    "_inkbox_dep",
    "_repos_dep",
    "decorate_opps",
    "router",
]
