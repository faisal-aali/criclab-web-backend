"""Data access for accounts, sessions, one-time codes, rate limits and audit.

Kept separate from `repository.py` (which serves the analysis pipeline) so the
account model can evolve without touching delivery storage.

Only digests of secrets are ever written here — see `app.core.security`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.security import digest
from app.db.mongo import get_db
from app.db.repository import new_id, utcnow

log = logging.getLogger("criclab.auth")

# Purposes a one-time code can serve. Codes are scoped so a verification code
# cannot be replayed against the password-reset endpoint.
OTP_VERIFY_EMAIL = "verify_email"
OTP_RESET_PASSWORD = "reset_password"

OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5


def normalise_email(email: str) -> str:
    return email.strip().lower()


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #

def public_user(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    """The shape of a user the API is allowed to return.

    An allow-list, not a deny-list: a field added to the stored document is not
    exposed until it is named here, so a future `password_hash`-like field
    cannot leak by being forgotten.
    """
    if not doc:
        return None
    return {
        "id": doc["_id"],
        "email": doc.get("email"),
        "name": doc.get("name"),
        "role": doc.get("role", "user"),
        "email_verified": bool(doc.get("email_verified")),
        "created_at": doc.get("created_at"),
        "profile": doc.get("profile") or {},
        "avatar_color": doc.get("avatar_color"),
    }


async def create_user(
    *, email: str, name: str, password_hash: str, role: str = "user"
) -> dict[str, Any] | None:
    """Insert a user. Returns None when the email is already taken.

    Uniqueness is decided by the index, not by a prior read — two simultaneous
    signups with the same address would both pass a read-then-write check.
    """
    doc = {
        "_id": new_id("usr"),
        "email": normalise_email(email),
        "name": name.strip(),
        "password_hash": password_hash,
        "role": role,
        "email_verified": False,
        "created_at": utcnow(),
        "updated_at": utcnow(),
        "profile": {},
        "avatar_color": _avatar_colour(email),
    }
    try:
        await get_db().users.insert_one(doc)
        log.debug(
            "create_user ok user_id=%s role=%s verified=%s",
            doc["_id"],
            doc["role"],
            doc["email_verified"],
        )
        return doc
    except DuplicateKeyError:
        log.debug("create_user duplicate email")
        return None


def _avatar_colour(email: str) -> str:
    """Stable accent per account, so an avatar is recognisable without a photo."""
    palette = ["#b6f24a", "#8fd12b", "#d9743c", "#4ade80", "#2f9e6b", "#7dd3fc"]
    return palette[sum(email.encode()) % len(palette)]


async def get_user_by_email(email: str) -> dict[str, Any] | None:
    return await get_db().users.find_one({"email": normalise_email(email)})


async def get_user(user_id: str) -> dict[str, Any] | None:
    return await get_db().users.find_one({"_id": user_id})


async def update_user(user_id: str, **fields: Any) -> dict[str, Any] | None:
    fields["updated_at"] = utcnow()
    return await get_db().users.find_one_and_update(
        {"_id": user_id}, {"$set": fields}, return_document=ReturnDocument.AFTER
    )


async def mark_email_verified(user_id: str) -> None:
    await update_user(user_id, email_verified=True, verified_at=utcnow())


async def set_password(user_id: str, password_hash: str) -> None:
    await update_user(user_id, password_hash=password_hash, password_changed_at=utcnow())


# --------------------------------------------------------------------------- #
# Sessions (refresh tokens)
# --------------------------------------------------------------------------- #

async def create_session(
    *, user_id: str, token_digest: str, days: int, user_agent: str = "", ip: str = ""
) -> dict[str, Any]:
    doc = {
        "_id": new_id("ses"),
        "user_id": user_id,
        "token_digest": token_digest,
        "created_at": utcnow(),
        "last_used_at": utcnow(),
        "expires_at": utcnow() + timedelta(days=days),
        "revoked_at": None,
        # Truncated: enough to recognise a device, not a full fingerprint.
        "user_agent": (user_agent or "")[:180],
        "ip": (ip or "")[:64],
    }
    await get_db().sessions.insert_one(doc)
    return doc


async def get_active_session(token: str) -> dict[str, Any] | None:
    """Look up a session by the plaintext refresh token."""
    doc = await get_db().sessions.find_one({"token_digest": digest(token)})
    if not doc or doc.get("revoked_at"):
        return None
    expires = doc.get("expires_at")
    if expires and _aware(expires) <= datetime.now(timezone.utc):
        return None
    return doc


def _aware(dt: datetime) -> datetime:
    """Mongo hands back naive UTC datetimes; comparisons need them aware."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def rotate_session(session_id: str, new_digest: str, days: int) -> None:
    """Swap in a fresh refresh token for the same session.

    Rotation means a stolen refresh token is usable at most once before the
    legitimate client's next renewal invalidates it.
    """
    await get_db().sessions.update_one(
        {"_id": session_id},
        {
            "$set": {
                "token_digest": new_digest,
                "last_used_at": utcnow(),
                "expires_at": utcnow() + timedelta(days=days),
            }
        },
    )


async def revoke_session(session_id: str) -> None:
    await get_db().sessions.update_one(
        {"_id": session_id}, {"$set": {"revoked_at": utcnow()}}
    )


async def revoke_all_sessions(user_id: str, *, except_id: str | None = None) -> int:
    """Sign out everywhere. Used after a password change."""
    query: dict[str, Any] = {"user_id": user_id, "revoked_at": None}
    if except_id:
        query["_id"] = {"$ne": except_id}
    result = await get_db().sessions.update_many(query, {"$set": {"revoked_at": utcnow()}})
    return int(result.modified_count)


async def list_sessions(user_id: str) -> list[dict[str, Any]]:
    cursor = (
        get_db()
        .sessions.find({"user_id": user_id, "revoked_at": None})
        .sort("last_used_at", -1)
        .limit(20)
    )
    return await cursor.to_list(length=20)


# --------------------------------------------------------------------------- #
# One-time codes
# --------------------------------------------------------------------------- #

async def create_otp(
    *, user_id: str, purpose: str, code_digest: str, minutes: int = OTP_TTL_MINUTES
) -> dict[str, Any]:
    """Issue a code, replacing any outstanding one for the same purpose.

    Replacing rather than accumulating means "resend" cannot be used to build a
    pool of simultaneously-valid codes.
    """
    await get_db().otps.delete_many({"user_id": user_id, "purpose": purpose})
    doc = {
        "_id": new_id("otp"),
        "user_id": user_id,
        "purpose": purpose,
        "code_digest": code_digest,
        "attempts": 0,
        "created_at": utcnow(),
        "expires_at": utcnow() + timedelta(minutes=minutes),
        "consumed_at": None,
    }
    await get_db().otps.insert_one(doc)
    log.debug(
        "otp stored user_id=%s purpose=%s ttl_min=%s",
        user_id,
        purpose,
        minutes,
    )
    return doc


async def consume_otp(*, user_id: str, purpose: str, code_digest: str) -> tuple[bool, str]:
    """Check and burn a code. Returns (ok, reason).

    A correct code is deleted on use, so it cannot be replayed. A wrong code
    increments the attempt counter and the record is destroyed once the limit is
    reached, forcing the user to request a new one.
    """
    db = get_db()
    doc = await db.otps.find_one({"user_id": user_id, "purpose": purpose})
    if not doc:
        return False, "That code has expired. Request a new one."
    if _aware(doc["expires_at"]) <= datetime.now(timezone.utc):
        await db.otps.delete_one({"_id": doc["_id"]})
        return False, "That code has expired. Request a new one."
    if doc.get("attempts", 0) >= OTP_MAX_ATTEMPTS:
        await db.otps.delete_one({"_id": doc["_id"]})
        return False, "Too many incorrect attempts. Request a new code."
    if doc["code_digest"] != code_digest:
        await db.otps.update_one({"_id": doc["_id"]}, {"$inc": {"attempts": 1}})
        left = OTP_MAX_ATTEMPTS - doc.get("attempts", 0) - 1
        return False, f"That code is not right. {max(left, 0)} attempts left."
    await db.otps.delete_one({"_id": doc["_id"]})
    return True, ""


async def recent_otp_age_seconds(user_id: str, purpose: str) -> float | None:
    """Seconds since the last code was issued, for resend throttling."""
    doc = await get_db().otps.find_one({"user_id": user_id, "purpose": purpose})
    if not doc:
        return None
    return (datetime.now(timezone.utc) - _aware(doc["created_at"])).total_seconds()


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #

async def hit_rate_limit(key: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
    """Count one attempt against `key`. Returns (allowed, remaining).

    Backed by Mongo rather than process memory so the limit holds across
    workers, and TTL-indexed so expired counters clean themselves up.
    """
    db = get_db()
    now = datetime.now(timezone.utc)
    doc = await db.rate_limits.find_one({"key": key})
    if not doc or _aware(doc["expires_at"]) <= now:
        await db.rate_limits.update_one(
            {"key": key},
            {"$set": {"count": 1, "expires_at": now + timedelta(seconds=window_seconds)}},
            upsert=True,
        )
        return True, limit - 1
    count = int(doc.get("count", 0)) + 1
    if count > limit:
        return False, 0
    await db.rate_limits.update_one({"key": key}, {"$set": {"count": count}})
    return True, limit - count


async def clear_rate_limit(key: str) -> None:
    """Reset a counter after the action it guards succeeds."""
    await get_db().rate_limits.delete_one({"key": key})


# --------------------------------------------------------------------------- #
# Security audit
# --------------------------------------------------------------------------- #

async def log_security_event(
    *, user_id: str | None, event: str, ip: str = "", user_agent: str = "", detail: str = ""
) -> None:
    """Record a security-relevant action.

    Deliberately records *what happened*, never the secret involved — no codes,
    tokens or passwords reach this collection.
    """
    try:
        await get_db().security_events.insert_one(
            {
                "_id": new_id("sec"),
                "user_id": user_id,
                "event": event,
                "ip": (ip or "")[:64],
                "user_agent": (user_agent or "")[:180],
                "detail": (detail or "")[:200],
                "created_at": utcnow(),
            }
        )
    except Exception as exc:
        log.warning("could not record security event %s: %s", event, type(exc).__name__)
