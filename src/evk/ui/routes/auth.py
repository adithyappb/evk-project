"""Authentication, registration, password reset, profile setup."""

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
from evk.models import AppUser, LoginChallenge, UserRole
from evk.ui.deps import (
    auth_dep,
    clear_session,
    current_user,
    redirect,
    repos_dep,
    settings_dep,
    set_session_cookie,
)
from evk.ui.helpers import allow_auth_resend, flash_redirect
from evk.ui.template_env import templates
from evk.ui.view_models import auth_page_context, role_home

router = APIRouter(tags=["ui-auth"])

@router.get("/", response_class=HTMLResponse, name="landing")
def landing(
    request: Request,
    current_user: AppUser | None = Depends(current_user),
) -> HTMLResponse:
    if current_user is not None:
        return redirect(request, "app_home")
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "request": request,
            "current_user": None,
            "wiring": describe_wiring(),
        },
    )


@router.get("/login", response_class=HTMLResponse, name="login_page")
def login_page(
    request: Request,
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
    flash: str | None = None,
) -> HTMLResponse:
    if current_user is not None:
        return redirect(request, "app_home")
    auth = AuthService(repos=repos, notifier=build_auth_notifier(get_settings()), settings=get_settings())
    auth.ensure_bootstrap()
    return templates.TemplateResponse(
        request,
        "landing.html",
        auth_page_context(request, auth_view="existing", flash=flash),
    )


@router.get("/register", response_class=HTMLResponse, name="register_page")
def register_page(
    request: Request,
    repos: Repos = Depends(repos_dep),
    current_user: AppUser | None = Depends(current_user),
) -> HTMLResponse:
    if current_user is not None:
        return redirect(request, "app_home")
    auth = AuthService(repos=repos, notifier=build_auth_notifier(get_settings()), settings=get_settings())
    auth.ensure_bootstrap()
    return templates.TemplateResponse(
        request,
        "landing.html",
        auth_page_context(request, auth_view="new"),
    )


@router.post("/auth/signup", response_class=HTMLResponse, name="auth_signup")
def auth_signup(
    request: Request,
    email: str = Form(...),
    name: str = Form(...),
    role: str = Form(...),
    access_key: str = Form(...),
    organization: str = Form(default=""),
    auth: AuthService = Depends(auth_dep),
) -> HTMLResponse:
    try:
        role_enum = UserRole(role)
        if role_enum is not UserRole.STUDENT:
            raise AuthError("Public signup is for students only. Staff accounts are created by an EVkids admin.")
        user = auth.create_user(
            email=email,
            name=name,
            role=role_enum,
            access_key=access_key,
            organization=organization,
        )
        _, dev_code = auth.start_login(email=user.email, access_key=access_key)
        return templates.TemplateResponse(
            request,
            "auth_verify.html",
            {
                "request": request,
                "current_user": None,
                "wiring": describe_wiring(),
                "email": user.email,
                "flash": "Account created. Enter the verification code to finish signing in.",
                "delivery_mode": auth.settings.auth_email_delivery_mode,
                "dev_code": dev_code,
            },
        )
    except (AuthError, ValueError) as exc:
        return templates.TemplateResponse(
            request,
            "landing.html",
            auth_page_context(request, auth_view="new", flash=str(exc)),
            status_code=400,
        )


@router.post("/auth/login", response_class=HTMLResponse, name="auth_login")
def auth_login(
    request: Request,
    email: str = Form(...),
    access_key: str = Form(...),
    auth: AuthService = Depends(auth_dep),
) -> HTMLResponse:
    try:
        user, dev_code = auth.start_login(email=email, access_key=access_key)
        return templates.TemplateResponse(
            request,
            "auth_verify.html",
            {
                "request": request,
                "current_user": None,
                "wiring": describe_wiring(),
                "email": user.email,
                "flash": "Verification code sent.",
                "delivery_mode": auth.settings.auth_email_delivery_mode,
                "dev_code": dev_code,
            },
        )
    except AuthError as exc:
        return templates.TemplateResponse(
            request,
            "landing.html",
            auth_page_context(request, auth_view="existing", flash=str(exc)),
            status_code=400,
        )


@router.post("/auth/verify", name="auth_verify", response_class=HTMLResponse)
def auth_verify(
    request: Request,
    email: str = Form(...),
    code: str = Form(...),
    auth: AuthService = Depends(auth_dep),
    settings: Settings = Depends(settings_dep),
) -> HTMLResponse:
    try:
        user, session = auth.verify_login(email=email, code=code)
    except AuthError as exc:
        return templates.TemplateResponse(
            request,
            "auth_verify.html",
            {
                "request": request,
                "current_user": None,
                "wiring": describe_wiring(),
                "email": email,
                "flash": str(exc),
                "delivery_mode": auth.settings.auth_email_delivery_mode,
            },
            status_code=400,
        )
    response = redirect(request, role_home(user))
    set_session_cookie(response, session.id, settings)
    return response


@router.post("/auth/resend", response_class=HTMLResponse, name="auth_resend")
def auth_resend(
    request: Request,
    email: str = Form(...),
    auth: AuthService = Depends(auth_dep),
) -> HTMLResponse:
    """Re-issue a login OTP for an email address — no access_key required.

    Security: we look up the user by email and only resend if an *existing*
    session challenge was already started (i.e. the user successfully passed
    step 1 within the last TTL window).  If no prior challenge exists we
    silently return the same page rather than revealing whether the email
    exists.
    """
    email_norm = email.strip().lower()
    if not allow_auth_resend(email_norm):
        return templates.TemplateResponse(
            request,
            "auth_verify.html",
            {
                "request": request,
                "current_user": None,
                "wiring": describe_wiring(),
                "email": email_norm,
                "flash": "Please wait a few minutes before requesting another code.",
                "delivery_mode": auth.settings.auth_email_delivery_mode,
            },
            status_code=429,
        )
    # Only resend if the user exists and is active — never reveal existence otherwise.
    user = auth.repos.users.get_by_email(email_norm)
    dev_code: str | None = None
    flash_msg = "A new code has been sent."

    if user is not None and user.is_active:
        import secrets
        from evk.auth import hash_login_code
        code = f"{secrets.randbelow(1_000_000):06d}"
        challenge = LoginChallenge(
            id=f"challenge_{user.id}_{secrets.token_hex(4)}",
            user_id=user.id,
            email=user.email,
            code_hash=hash_login_code(code, user_id=user.id),
            expires_at=datetime.now(UTC) + timedelta(minutes=auth.settings.login_code_ttl_minutes),
            purpose="login",
        )
        auth.repos.login_challenges.upsert(challenge)
        auth.notifier.send_code(email=user.email, code=code)
        if isinstance(auth.notifier, TerminalAuthNotifier):
            dev_code = auth.notifier.last_code  # type: ignore[attr-defined]

    return templates.TemplateResponse(
        request,
        "auth_verify.html",
        {
            "request": request,
            "current_user": None,
            "wiring": describe_wiring(),
            "email": email_norm,
            "flash": flash_msg,
            "delivery_mode": auth.settings.auth_email_delivery_mode,
            "dev_code": dev_code,
        },
    )


@router.get("/forgot", response_class=HTMLResponse, name="forgot_page")
def forgot_page(
    request: Request,
    current_user: AppUser | None = Depends(current_user),
) -> HTMLResponse:
    if current_user is not None:
        return redirect(request, "app_home")
    return templates.TemplateResponse(
        request,
        "forgot.html",
        {
            "request": request,
            "current_user": None,
            "wiring": describe_wiring(),
            "flash": None,
        },
    )


@router.post("/auth/forgot", response_class=HTMLResponse, name="auth_forgot")
def auth_forgot(
    request: Request,
    email: str = Form(...),
    auth: AuthService = Depends(auth_dep),
) -> HTMLResponse:
    dev_code = auth.start_reset(email=email)
    return templates.TemplateResponse(
        request,
        "reset_verify.html",
        {
            "request": request,
            "current_user": None,
            "wiring": describe_wiring(),
            "email": email.strip().lower(),
            "flash": None,
            "dev_code": dev_code,
            "delivery_mode": auth.settings.auth_email_delivery_mode,
        },
    )


@router.post("/auth/reset", response_class=HTMLResponse, name="auth_reset")
def auth_reset(
    request: Request,
    email: str = Form(...),
    code: str = Form(...),
    new_access_key: str = Form(...),
    auth: AuthService = Depends(auth_dep),
) -> HTMLResponse:
    try:
        auth.complete_reset(email=email, code=code, new_access_key=new_access_key)
    except AuthError as exc:
        return templates.TemplateResponse(
            request,
            "reset_verify.html",
            {
                "request": request,
                "current_user": None,
                "wiring": describe_wiring(),
                "email": email,
                "flash": str(exc),
                "dev_code": None,
                "delivery_mode": auth.settings.auth_email_delivery_mode,
            },
            status_code=400,
        )
    response = redirect(request, "login_page")
    # Carry flash message via query param so landing.html can show it
    from fastapi.responses import RedirectResponse as _RR
    url = str(request.url_for("login_page")) + "?flash=Password+updated+%E2%80%94+sign+in+with+your+new+password."
    return _RR(url, status_code=303)


@router.post("/auth/logout", name="logout")
def logout(
    request: Request,
    auth: AuthService = Depends(auth_dep),
    settings: Settings = Depends(settings_dep),
) -> RedirectResponse:
    auth.revoke_session(request.cookies.get(settings.session_cookie_name))
    response = redirect(request, "landing")
    clear_session(response, settings)
    return response


@router.get("/profile/setup", response_class=HTMLResponse, name="profile_setup_page")
def profile_setup_page(
    request: Request,
    token: str = "",
    email: str = "",
    repos: Repos = Depends(repos_dep),
    flash: str = "",
) -> HTMLResponse:
    from datetime import datetime as _dt
    user = repos.users.get_by_email(email.strip().lower()) if email else None
    if not user or user.activation_token != token:
        return templates.TemplateResponse(
            request,
            "profile_setup.html",
            {"request": request, "valid": False, "email": email,
             "flash": "This link is invalid or has expired.",
             "current_user": None, "wiring": describe_wiring()},
        )
    expires = user.activation_token_expires
    if expires:
        exp_dt = _dt.fromisoformat(str(expires)) if isinstance(expires, str) else expires
        if exp_dt.replace(tzinfo=UTC) < datetime.now(UTC):
            return templates.TemplateResponse(
                request,
                "profile_setup.html",
                {"request": request, "valid": False, "email": email,
                 "flash": "This link has expired. Ask an admin to resend your welcome email.",
                 "current_user": None, "wiring": describe_wiring()},
            )
    return templates.TemplateResponse(
        request,
        "profile_setup.html",
        {"request": request, "valid": True, "email": email, "token": token,
         "flash": flash, "current_user": None, "wiring": describe_wiring()},
    )


@router.post("/profile/setup", name="profile_setup_submit")
def profile_setup_submit(
    request: Request,
    email: str = Form(...),
    token: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    career_interests: list[str] = Form(default=[]),
    opportunity_types: list[str] = Form(default=[]),
    notification_frequency: str = Form("weekly"),
    repos: Repos = Depends(repos_dep),
) -> HTMLResponse:
    user = repos.users.get_by_email(email.strip().lower())
    if not user or user.activation_token != token:
        raise HTTPException(status_code=400, detail="Invalid token")
    if password != confirm_password or len(password) < 8:
        return templates.TemplateResponse(
            request,
            "profile_setup.html",
            {"request": request, "valid": True, "email": email, "token": token,
             "flash": "Passwords must match and be at least 8 characters.",
             "current_user": None, "wiring": describe_wiring()},
        )
    import secrets as _s
    from evk.auth import hash_access_key
    salt = _s.token_hex(8)
    repos.users.patch(user.id, {
        "access_key_salt": salt,
        "access_key_hash": hash_access_key(password, salt=salt),
        "is_active": True,
        "activation_token": None,
        "activation_token_expires": None,
    })
    if user.student_id:
        repos.students.patch(user.student_id, {
            "career_interests": career_interests,
            "opportunity_types_sought": opportunity_types,
            "notification_frequency": notification_frequency,
            "opted_in": True,
        })
    return flash_redirect(request, "login_page", "Profile ready — sign in with your new password.")

