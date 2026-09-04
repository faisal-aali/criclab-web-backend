"""Password hashing, token minting and OTP generation.

Everything security-sensitive that is pure computation lives here, so the
services and routes above it never touch a hash, a signing key or a raw token
value directly.

Design notes that matter:

* Passwords are bcrypt-hashed with a per-password salt. bcrypt truncates at 72
  bytes, so input is bounded before hashing rather than silently cut.
* Access tokens are short-lived JWTs. Refresh tokens are NOT JWTs — they are
  opaque random strings stored only as SHA-256 digests, so a database leak does
  not hand over usable sessions, and any single token can be revoked.
* OTPs are likewise stored as digests, never in plaintext.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import bcrypt
import jwt

from app.config import get_settings

TokenType = Literal["access", "refresh"]

# bcrypt only considers the first 72 bytes; longer input would be silently
# truncated, which makes two different long passwords equivalent.
MAX_PASSWORD_BYTES = 72
MIN_PASSWORD_LENGTH = 8

# Not a full breach list — just the stems that a trivial wordlist tries first.
# The real defence is rate limiting and bcrypt; this only stops the worst picks.
COMMON_PASSWORDS = {
    "password", "passw0rd", "qwerty", "qwertyui", "letmein", "welcome",
    "iloveyou", "admin", "abc", "abcd", "test", "changeme", "secret",
    "cricket", "criclab", "bowling", "monkey", "dragon", "football",
}


class TokenError(Exception):
    """Raised when a token is malformed, expired, or of the wrong type."""


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #

def hash_password(password: str) -> str:
    raw = password.encode("utf-8")[:MAX_PASSWORD_BYTES]
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, hashed: str | None) -> bool:
    """Constant-time check that also runs when there is no stored hash.

    A missing hash still costs a bcrypt round: returning early would make
    "no such account" measurably faster than "wrong password", which is how
    account enumeration by timing works.
    """
    raw = password.encode("utf-8")[:MAX_PASSWORD_BYTES]
    if not hashed:
        bcrypt.hashpw(raw, bcrypt.gensalt(rounds=12))
        return False
    try:
        return bcrypt.checkpw(raw, hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def password_problems(password: str) -> list[str]:
    """Human-readable reasons a password is unacceptable. Empty list = fine."""
    problems: list[str] = []
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(f"Use at least {MIN_PASSWORD_LENGTH} characters")
    if not any(c.isalpha() for c in password):
        problems.append("Include at least one letter")
    if not any(c.isdigit() for c in password):
        problems.append("Include at least one number")
    # Strip trailing digits before the check: "password1" and "password123" are
    # the same guess as "password" to anyone running a wordlist.
    stem = password.lower().rstrip("0123456789!@#$")
    if password.lower() in COMMON_PASSWORDS or stem in COMMON_PASSWORDS:
        problems.append("That password is too easy to guess")
    return problems


# --------------------------------------------------------------------------- #
# Access tokens (JWT)
# --------------------------------------------------------------------------- #

def create_access_token(
    user_id: str,
    *,
    email: str,
    role: str = "user",
    minutes: int | None = None,
    pwd_at: int = 0,
) -> tuple[str, datetime]:
    """Sign a short-lived access token. Returns (token, expiry).

    `pwd_at` stamps the account's password-change marker into the token. The
    guard compares it against the account's current value, which invalidates
    every token minted before a password change *exactly* — comparing `iat`
    against a timestamp cannot, because JWT issues at whole-second resolution
    and a replacement token minted in the same second is indistinguishable from
    the token being replaced.
    """
    settings = get_settings()
    if not settings.jwt_secret:
        raise TokenError("JWT_SECRET is not configured")
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=minutes or settings.access_token_minutes)
    payload: dict[str, Any] = {
        "sub": user_id,
        "email": email,
        "role": role,
        "type": "access",
        "pwd_at": pwd_at,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        # Distinguishes tokens minted in different sessions in the audit log.
        "jti": secrets.token_urlsafe(8),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm), expires


def decode_access_token(token: str) -> dict[str, Any]:
    """Verify and decode an access token, or raise TokenError.

    Every failure mode is collapsed into one exception type with a message the
    caller can safely show: the client only ever needs to know it should
    refresh or sign in again.
    """
    settings = get_settings()
    if not settings.jwt_secret:
        raise TokenError("JWT_SECRET is not configured")
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Session expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("Invalid session") from exc
    if payload.get("type") != "access":
        # A refresh token presented as a bearer token must not be accepted.
        raise TokenError("Wrong token type")
    if not payload.get("sub"):
        raise TokenError("Invalid session")
    return payload


# --------------------------------------------------------------------------- #
# Refresh tokens + OTPs — opaque secrets, stored only as digests
# --------------------------------------------------------------------------- #

def new_refresh_token() -> tuple[str, str]:
    """Return (plaintext, digest). Only the digest is ever persisted."""
    token = secrets.token_urlsafe(48)
    return token, digest(token)


def digest(value: str) -> str:
    """SHA-256 of a bearer-style secret.

    Deliberately not bcrypt: these values are already 256+ bits of entropy, so
    they are not brute-forceable and do not need a slow KDF — and refresh/OTP
    checks happen on hot paths where a bcrypt round per request would hurt.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def digests_match(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


def new_otp(length: int = 6) -> str:
    """A numeric one-time code. Zero-padded so every code is the same length."""
    upper = 10**length
    return str(secrets.randbelow(upper)).zfill(length)


def new_url_token() -> tuple[str, str]:
    """Single-use token for a link in an email. Returns (plaintext, digest)."""
    token = secrets.token_urlsafe(32)
    return token, digest(token)
