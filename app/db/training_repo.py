from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.db.mongo import get_db


def _col():
    return get_db()["training_plans"]


async def get_plan(user_id: str) -> dict[str, Any] | None:
    return await _col().find_one({"user_id": user_id})


async def upsert_plan(user_id: str, **fields: Any) -> None:
    now = datetime.now(timezone.utc)
    await _col().update_one(
        {"user_id": user_id},
        {
            "$set": {**fields, "updated_at": now},
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


async def mark_generating(user_id: str, delivery_count: int) -> None:
    """Record that a plan generation has started for this user."""
    now = datetime.now(timezone.utc)
    await _col().update_one(
        {"user_id": user_id},
        {
            "$set": {
                "generation_status": "generating",
                "delivery_count": delivery_count,
                "started_at": now,
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


async def mark_ready(user_id: str, plan: dict[str, Any], delivery_count: int) -> None:
    """Store a finished plan and clear the generating flag."""
    now = datetime.now(timezone.utc)
    await _col().update_one(
        {"user_id": user_id},
        {
            "$set": {
                "generation_status": "ready",
                "plan": plan,
                "delivery_count": delivery_count,
                "generated_at": now,
                "updated_at": now,
            },
            "$unset": {"started_at": "", "error": ""},
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


async def mark_failed(user_id: str, error: str) -> None:
    """Mark a generation as failed without erasing a previous plan."""
    now = datetime.now(timezone.utc)
    await _col().update_one(
        {"user_id": user_id},
        {
            "$set": {
                "generation_status": "failed",
                "error": error,
                "updated_at": now,
            },
            "$unset": {"started_at": ""},
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


def is_generation_active(doc: dict[str, Any] | None, *, stale_seconds: int = 120) -> bool:
    """Return True if `doc` says a generation is still running and not stale."""
    if not doc:
        return False
    if doc.get("generation_status") != "generating":
        return False
    started_at = doc.get("started_at")
    if not isinstance(started_at, datetime):
        return False
    return datetime.now(timezone.utc) - started_at < timedelta(seconds=stale_seconds)
