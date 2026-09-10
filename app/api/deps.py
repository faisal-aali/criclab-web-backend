"""Request-scoped dependencies: authentication, authorisation, client context.

Route handlers never parse a token themselves. They declare what they need —
`CurrentUser`, `VerifiedUser`, `AdminUser` — and get a user document or a 401/403.

The distinction between the three matters:

* `CurrentUser`   signed in. Enough to read your own account or verify email.
* `VerifiedUser`  signed in AND email confirmed. Required for anything that
                  creates data or sends mail on the user's behalf.
* `AdminUser`     staff-only operations.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request, status

from app.core.security import TokenError, decode_access_token
from app.db import auth_repo


def password_marker(user: dict[str, Any]) -> int:
    """Whole-second stamp of the last password change; 0 if never changed."""
    changed = user.get("password_changed_at")
    return int(changed.timestamp()) if changed else 0


class ClientContext:
    """Where a request came from — for audit rows and session records."""

    __slots__ = ("ip", "user_agent")

    def __init__(self, ip: str, user_agent: str) -> None:
        self.ip = ip
        self.user_agent = user_agent


def get_client(request: Request) -> ClientContext:
    # X-Forwarded-For is only meaningful behind a proxy that sets it; the first
    # entry is the original client when the chain is trusted.
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "")
    return ClientContext(ip=ip, user_agent=request.headers.get("user-agent", ""))


Client = Annotated[ClientContext, Depends(get_client)]


def _unauthorised(detail: str) -> HTTPException:
    # WWW-Authenticate lets the client tell "refresh me" from "sign in again".
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def current_user(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Resolve the signed-in user from a bearer token, or raise 401."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _unauthorised("Sign in to continue")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = decode_access_token(token)
    except TokenError as exc:
        raise _unauthorised(str(exc)) from exc

    user = await auth_repo.get_user(payload["sub"])
    if not user:
        # Token signature was valid but the account is gone.
        raise _unauthorised("Account no longer exists")
    if user.get("disabled"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has been disabled")

    # A token minted before the last password change is no longer acceptable,
    # even if it has not expired — this is what makes "sign out everywhere" real
    # for access tokens, which are not stored server-side. The token carries the
    # marker it was minted against; anything stale simply will not match.
    if int(payload.get("pwd_at") or 0) != password_marker(user):
        raise _unauthorised("Session expired")

    return user


CurrentUser = Annotated[dict[str, Any], Depends(current_user)]


async def verified_user(user: CurrentUser) -> dict[str, Any]:
    """Signed in and email confirmed."""
    if not user.get("email_verified"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Confirm your email address to use this",
        )
    return user


VerifiedUser = Annotated[dict[str, Any], Depends(verified_user)]


async def admin_user(user: CurrentUser) -> dict[str, Any]:
    if user.get("role") != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have access to this")
    return user


AdminUser = Annotated[dict[str, Any], Depends(admin_user)]


def visible_to(user: dict[str, Any], owner_id: str | None, *, exists: bool) -> bool:
    """Whether this caller may see a resource.

    A regular user is told 404 both when the row is missing and when it
    belongs to someone else — probing ids must not reveal that a delivery
    exists. Staff may read any row that exists, including pre-launch data
    that was never attributed (`user_id` absent).
    """
    if not exists:
        return False
    if user.get("role") == "admin":
        return True
    return owner_id == user["_id"]



async def optional_user(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any] | None:
    """The user if signed in, otherwise None — never raises.

    For endpoints that work for anonymous visitors but personalise when signed
    in, such as the assistant.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    try:
        payload = decode_access_token(authorization.split(" ", 1)[1].strip())
    except TokenError:
        return None
    user = await auth_repo.get_user(payload["sub"])
    # Same acceptance rules as `current_user`, minus the 401: a disabled
    # account or a token minted before the last password change is treated
    # as anonymous rather than as a signed-in caller.
    if not user or user.get("disabled"):
        return None
    if int(payload.get("pwd_at") or 0) != password_marker(user):
        return None
    return user


OptionalUser = Annotated[dict[str, Any] | None, Depends(optional_user)]


async def enforce_rate_limit(
    key: str, *, limit: int, window_seconds: int, message: str
) -> None:
    """Raise 429 when `key` has exceeded its allowance in the window."""
    allowed, _ = await auth_repo.hit_rate_limit(key, limit=limit, window_seconds=window_seconds)
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            message,
            headers={"Retry-After": str(window_seconds)},
        )
