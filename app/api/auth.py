"""Account lifecycle: signup, verification, sign-in, renewal, recovery, profile.

Two conventions run through every handler here.

**Never confirm whether an address has an account.** Signup, sign-in and
password recovery all answer the same way whether or not the email is known.
Anything else turns these endpoints into a membership oracle.

**Sending email never decides whether the request succeeded.** The mail server
being unreachable must not fail a signup — the account exists and the code can
be resent.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from app.api.deps import Client, CurrentUser, enforce_rate_limit, password_marker
from app.config import get_settings
from app.core.security import (
    create_access_token,
    digest,
    hash_password,
    new_otp,
    new_refresh_token,
    password_problems,
    verify_password,
)
from app.db import auth_repo
from app.services import email_service, notification_service

log = logging.getLogger("criclab.auth")
router = APIRouter(prefix="/auth", tags=["auth"])

# Deliberately identical for "unknown address" and "wrong password".
BAD_CREDENTIALS = "That email or password is not right"
# Returned by every recovery request, found or not.
RECOVERY_SENT = "If that address has a CricLab account, a reset code is on its way."


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #

class SignUpIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)


class SignInIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class OtpIn(BaseModel):
    email: EmailStr
    code: str = Field(min_length=4, max_length=8)


class EmailIn(BaseModel):
    email: EmailStr


class ResetIn(BaseModel):
    email: EmailStr
    code: str = Field(min_length=4, max_length=8)
    password: str = Field(min_length=8, max_length=200)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=8, max_length=200)


class ProfileIn(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    profile: dict[str, Any] | None = None


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=10, max_length=400)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _reject_weak_password(password: str) -> None:
    problems = password_problems(password)
    if problems:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "; ".join(problems))


async def _issue_tokens(user: dict[str, Any], client: Client) -> dict[str, Any]:
    """Mint an access token and open a refresh session."""
    settings = get_settings()
    access, expires = create_access_token(
        user["_id"], email=user["email"], role=user.get("role", "user"),
        pwd_at=password_marker(user),
    )
    refresh_plain, refresh_digest = new_refresh_token()
    await auth_repo.create_session(
        user_id=user["_id"],
        token_digest=refresh_digest,
        days=settings.refresh_token_days,
        user_agent=client.user_agent,
        ip=client.ip,
    )
    return {
        "access_token": access,
        "refresh_token": refresh_plain,
        "token_type": "bearer",
        "expires_at": expires.isoformat(),
        "expires_in": settings.access_token_minutes * 60,
        "user": auth_repo.public_user(user),
    }


async def _send_verification(user: dict[str, Any], tasks: BackgroundTasks) -> None:
    code = new_otp()
    await auth_repo.create_otp(
        user_id=user["_id"],
        purpose=auth_repo.OTP_VERIFY_EMAIL,
        code_digest=digest(code),
        minutes=auth_repo.OTP_TTL_MINUTES,
    )
    tasks.add_task(
        email_service.send_verification_otp,
        user["email"],
        user.get("name") or "there",
        code,
        auth_repo.OTP_TTL_MINUTES,
    )


def _describe_device(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    for token, label in (
        ("iphone", "iPhone"), ("ipad", "iPad"), ("android", "Android"),
        ("mac os", "Mac"), ("windows", "Windows"), ("linux", "Linux"),
    ):
        if token in ua:
            return label
    return "a new device"


# --------------------------------------------------------------------------- #
# Sign up + verification
# --------------------------------------------------------------------------- #

@router.post("/signup", status_code=status.HTTP_201_CREATED)
async def sign_up(body: SignUpIn, tasks: BackgroundTasks, client: Client):
    await enforce_rate_limit(
        f"signup:{client.ip}", limit=10, window_seconds=3600,
        message="Too many sign-up attempts. Try again in an hour.",
    )
    _reject_weak_password(body.password)

    user = await auth_repo.create_user(
        email=body.email, name=body.name, password_hash=hash_password(body.password)
    )
    if user is None:
        # The address is taken. Do not say so — mail the existing owner instead,
        # so a real owner is told and a prober learns nothing.
        existing = await auth_repo.get_user_by_email(body.email)
        if existing:
            await auth_repo.log_security_event(
                user_id=existing["_id"], event="signup_existing_email",
                ip=client.ip, user_agent=client.user_agent,
            )
            if not existing.get("email_verified"):
                await _send_verification(existing, tasks)
        return {
            "status": "pending_verification",
            "message": "Check your email for a 6-digit code to confirm your account.",
            "email": auth_repo.normalise_email(body.email),
        }

    await _send_verification(user, tasks)
    await auth_repo.log_security_event(
        user_id=user["_id"], event="signup", ip=client.ip, user_agent=client.user_agent
    )
    return {
        "status": "pending_verification",
        "message": "Check your email for a 6-digit code to confirm your account.",
        "email": user["email"],
    }


@router.post("/verify-email")
async def verify_email(body: OtpIn, tasks: BackgroundTasks, client: Client):
    await enforce_rate_limit(
        f"verify:{client.ip}", limit=20, window_seconds=900,
        message="Too many attempts. Try again shortly.",
    )
    user = await auth_repo.get_user_by_email(body.email)
    if not user:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That code has expired. Request a new one.")
    if user.get("email_verified"):
        return {"status": "already_verified", **await _issue_tokens(user, client)}

    ok, reason = await auth_repo.consume_otp(
        user_id=user["_id"], purpose=auth_repo.OTP_VERIFY_EMAIL, code_digest=digest(body.code)
    )
    if not ok:
        await auth_repo.log_security_event(
            user_id=user["_id"], event="verify_failed", ip=client.ip, user_agent=client.user_agent
        )
        raise HTTPException(status.HTTP_400_BAD_REQUEST, reason)

    await auth_repo.mark_email_verified(user["_id"])
    user = await auth_repo.get_user(user["_id"]) or user
    await auth_repo.log_security_event(
        user_id=user["_id"], event="email_verified", ip=client.ip, user_agent=client.user_agent
    )
    tasks.add_task(email_service.send_welcome, user["email"], user.get("name") or "there")
    await notification_service.notify(
        user_id=user["_id"], kind="account",
        title="Email confirmed",
        body="Your account is fully set up. Film a delivery to get your first analysis.",
        link="/app/action",
    )
    return {"status": "verified", **await _issue_tokens(user, client)}


@router.post("/resend-otp")
async def resend_otp(
    body: EmailIn,
    tasks: BackgroundTasks,
    client: Client,
    purpose: Annotated[str, Body(embed=True)] = auth_repo.OTP_VERIFY_EMAIL,
):
    if purpose not in (auth_repo.OTP_VERIFY_EMAIL, auth_repo.OTP_RESET_PASSWORD):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown request")
    await enforce_rate_limit(
        f"resend:{auth_repo.normalise_email(body.email)}", limit=5, window_seconds=900,
        message="A code was just sent. Wait a few minutes before asking for another.",
    )
    user = await auth_repo.get_user_by_email(body.email)
    generic = {"status": "sent", "message": "If that account exists, a new code is on its way."}
    if not user:
        return generic

    # Cheap flood guard on top of the window limit: refuse a second code within
    # 45 seconds even if the window allows it.
    age = await auth_repo.recent_otp_age_seconds(user["_id"], purpose)
    if age is not None and age < 45:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"A code was sent moments ago. Try again in {int(45 - age)} seconds.",
        )

    code = new_otp()
    await auth_repo.create_otp(
        user_id=user["_id"], purpose=purpose, code_digest=digest(code),
        minutes=auth_repo.OTP_TTL_MINUTES,
    )
    name = user.get("name") or "there"
    if purpose == auth_repo.OTP_VERIFY_EMAIL:
        tasks.add_task(email_service.send_verification_otp, user["email"], name, code, auth_repo.OTP_TTL_MINUTES)
    else:
        tasks.add_task(email_service.send_password_reset_otp, user["email"], name, code, auth_repo.OTP_TTL_MINUTES)
    return generic


# --------------------------------------------------------------------------- #
# Sign in / out / renew
# --------------------------------------------------------------------------- #

@router.post("/login")
async def sign_in(body: SignInIn, tasks: BackgroundTasks, client: Client):
    # Two limits: one per address so a targeted attack is slowed, one per IP so
    # spraying many addresses from one host is slowed too.
    email = auth_repo.normalise_email(body.email)
    await enforce_rate_limit(
        f"login:{email}", limit=8, window_seconds=900,
        message="Too many sign-in attempts for this account. Try again in 15 minutes.",
    )
    await enforce_rate_limit(
        f"login-ip:{client.ip}", limit=40, window_seconds=900,
        message="Too many sign-in attempts. Try again shortly.",
    )

    user = await auth_repo.get_user_by_email(email)
    # Always runs a bcrypt comparison, present or not — see verify_password.
    if not verify_password(body.password, (user or {}).get("password_hash")):
        await auth_repo.log_security_event(
            user_id=(user or {}).get("_id"), event="login_failed",
            ip=client.ip, user_agent=client.user_agent,
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, BAD_CREDENTIALS)
    if user.get("disabled"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has been disabled")

    await auth_repo.clear_rate_limit(f"login:{email}")

    if not user.get("email_verified"):
        # Not an error: the client routes to the verification screen.
        await _send_verification(user, tasks)
        return {
            "status": "pending_verification",
            "message": "Confirm your email to finish signing in. We have sent a new code.",
            "email": user["email"],
        }

    await auth_repo.log_security_event(
        user_id=user["_id"], event="login", ip=client.ip, user_agent=client.user_agent
    )
    # Read by the admin dashboard's "active users" metric — a simple stamp
    # rather than a session log, since only "when last" is ever asked of it.
    await auth_repo.update_user(user["_id"], last_login_at=datetime.now(timezone.utc))
    sessions = await auth_repo.list_sessions(user["_id"])
    if len(sessions) >= 1:
        # Only worth mentioning when there was already a session elsewhere.
        tasks.add_task(
            email_service.send_new_login_alert,
            user["email"], user.get("name") or "there",
            datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC"),
            _describe_device(client.user_agent),
        )
    return {"status": "ok", **await _issue_tokens(user, client)}


@router.post("/refresh")
async def refresh(body: RefreshIn, client: Client):
    """Exchange a refresh token for a new access token, rotating the refresh token."""
    session = await auth_repo.get_active_session(body.refresh_token)
    if not session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired. Sign in again.")
    user = await auth_repo.get_user(session["user_id"])
    if not user or user.get("disabled"):
        await auth_repo.revoke_session(session["_id"])
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired. Sign in again.")

    settings = get_settings()
    access, expires = create_access_token(
        user["_id"], email=user["email"], role=user.get("role", "user"),
        pwd_at=password_marker(user),
    )
    new_plain, new_digest = new_refresh_token()
    await auth_repo.rotate_session(session["_id"], new_digest, settings.refresh_token_days)
    return {
        "access_token": access,
        "refresh_token": new_plain,
        "token_type": "bearer",
        "expires_at": expires.isoformat(),
        "expires_in": settings.access_token_minutes * 60,
        "user": auth_repo.public_user(user),
    }


@router.post("/logout")
async def sign_out(body: RefreshIn, client: Client):
    """Revoke one session. Idempotent — an unknown token is still a success."""
    session = await auth_repo.get_active_session(body.refresh_token)
    if session:
        await auth_repo.revoke_session(session["_id"])
        await auth_repo.log_security_event(
            user_id=session["user_id"], event="logout", ip=client.ip, user_agent=client.user_agent
        )
    return {"status": "ok"}


@router.post("/logout-all")
async def sign_out_everywhere(user: CurrentUser, client: Client):
    count = await auth_repo.revoke_all_sessions(user["_id"])
    await auth_repo.log_security_event(
        user_id=user["_id"], event="logout_all", ip=client.ip, user_agent=client.user_agent
    )
    return {"status": "ok", "sessions_ended": count}


# --------------------------------------------------------------------------- #
# Password recovery + change
# --------------------------------------------------------------------------- #

@router.post("/forgot-password")
async def forgot_password(body: EmailIn, tasks: BackgroundTasks, client: Client):
    email = auth_repo.normalise_email(body.email)
    await enforce_rate_limit(
        f"forgot:{email}", limit=5, window_seconds=900,
        message="A reset code was just sent. Check your inbox before asking for another.",
    )
    user = await auth_repo.get_user_by_email(email)
    if user:
        code = new_otp()
        await auth_repo.create_otp(
            user_id=user["_id"], purpose=auth_repo.OTP_RESET_PASSWORD,
            code_digest=digest(code), minutes=auth_repo.OTP_TTL_MINUTES,
        )
        tasks.add_task(
            email_service.send_password_reset_otp,
            user["email"], user.get("name") or "there", code, auth_repo.OTP_TTL_MINUTES,
        )
        await auth_repo.log_security_event(
            user_id=user["_id"], event="password_reset_requested",
            ip=client.ip, user_agent=client.user_agent,
        )
    return {"status": "sent", "message": RECOVERY_SENT}


@router.post("/reset-password")
async def reset_password(body: ResetIn, tasks: BackgroundTasks, client: Client):
    await enforce_rate_limit(
        f"reset:{client.ip}", limit=20, window_seconds=900,
        message="Too many attempts. Try again shortly.",
    )
    _reject_weak_password(body.password)
    user = await auth_repo.get_user_by_email(body.email)
    if not user:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That code has expired. Request a new one.")

    ok, reason = await auth_repo.consume_otp(
        user_id=user["_id"], purpose=auth_repo.OTP_RESET_PASSWORD, code_digest=digest(body.code)
    )
    if not ok:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, reason)

    await auth_repo.set_password(user["_id"], hash_password(body.password))
    # A reset is a recovery from possible compromise: end every existing session.
    await auth_repo.revoke_all_sessions(user["_id"])
    if not user.get("email_verified"):
        # Receiving the code proves control of the address.
        await auth_repo.mark_email_verified(user["_id"])
    await auth_repo.log_security_event(
        user_id=user["_id"], event="password_reset", ip=client.ip, user_agent=client.user_agent
    )
    tasks.add_task(email_service.send_password_changed, user["email"], user.get("name") or "there")
    await notification_service.notify(
        user_id=user["_id"], kind="security", title="Password changed",
        body="Your password was reset and you were signed out on all devices.",
        link="/app/settings",
    )
    return {"status": "ok", "message": "Password updated. Sign in with your new password."}


@router.post("/change-password")
async def change_password(
    body: ChangePasswordIn, user: CurrentUser, tasks: BackgroundTasks, client: Client
):
    if not verify_password(body.current_password, user.get("password_hash")):
        await auth_repo.log_security_event(
            user_id=user["_id"], event="password_change_failed",
            ip=client.ip, user_agent=client.user_agent,
        )
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Your current password is not right")
    _reject_weak_password(body.new_password)
    if verify_password(body.new_password, user.get("password_hash")):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Choose a different password")

    await auth_repo.set_password(user["_id"], hash_password(body.new_password))
    ended = await auth_repo.revoke_all_sessions(user["_id"])
    await auth_repo.log_security_event(
        user_id=user["_id"], event="password_changed", ip=client.ip, user_agent=client.user_agent
    )
    tasks.add_task(email_service.send_password_changed, user["email"], user.get("name") or "there")
    await notification_service.notify(
        user_id=user["_id"], kind="security", title="Password changed",
        body="Your password was updated. Other devices have been signed out.",
        link="/app/settings",
    )
    # The caller's own access token is now stale too (see deps.current_user), so
    # hand back a fresh pair rather than making them sign in again — minted from
    # the *reloaded* account, so it carries the new password marker.
    refreshed = await auth_repo.get_user(user["_id"]) or user
    return {"status": "ok", "sessions_ended": ended, **await _issue_tokens(refreshed, client)}


# --------------------------------------------------------------------------- #
# Account
# --------------------------------------------------------------------------- #

@router.get("/me")
async def me(user: CurrentUser):
    return {"user": auth_repo.public_user(user)}


@router.patch("/me")
async def update_me(body: ProfileIn, user: CurrentUser):
    fields: dict[str, Any] = {}
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Name cannot be empty")
        fields["name"] = name
    if body.profile is not None:
        # Merged rather than replaced, so a partial save cannot wipe the rest.
        merged = {**(user.get("profile") or {}), **body.profile}
        fields["profile"] = merged
    if not fields:
        return {"user": auth_repo.public_user(user)}
    updated = await auth_repo.update_user(user["_id"], **fields)
    return {"user": auth_repo.public_user(updated)}


@router.get("/sessions")
async def my_sessions(user: CurrentUser):
    """Devices currently signed in. No token material is included."""
    rows = await auth_repo.list_sessions(user["_id"])
    return {
        "items": [
            {
                "id": s["_id"],
                "created_at": s.get("created_at"),
                "last_used_at": s.get("last_used_at"),
                "device": _describe_device(s.get("user_agent", "")),
            }
            for s in rows
        ]
    }


@router.delete("/sessions/{session_id}")
async def end_session(session_id: str, user: CurrentUser):
    rows = await auth_repo.list_sessions(user["_id"])
    if not any(s["_id"] == session_id for s in rows):
        # Ownership check: never let one account revoke another's session.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    await auth_repo.revoke_session(session_id)
    return {"status": "ok"}
