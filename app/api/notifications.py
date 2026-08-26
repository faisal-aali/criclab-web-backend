"""Notification centre endpoints.

Every handler is scoped to the caller: `user_id` is part of the query filter,
never a parameter the client can supply.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import CurrentUser
from app.services import notification_service as svc

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("")
async def list_notifications(
    user: CurrentUser,
    limit: int = Query(20, ge=1, le=50),
    before: str | None = Query(None, description="Id to page back from"),
    unread_only: bool = Query(False),
):
    rows = await svc.list_for_user(user["_id"], limit=limit, before=before, unread_only=unread_only)
    return {
        "items": [svc.public_notification(r) for r in rows],
        "unread": await svc.unread_count(user["_id"]),
        # Cursor for the next page; absent when this was the last one.
        "next_cursor": rows[-1]["_id"] if len(rows) == limit else None,
    }


@router.get("/unread-count")
async def get_unread_count(user: CurrentUser):
    """Polled by the header badge — kept separate so it stays a counted index hit."""
    return {"unread": await svc.unread_count(user["_id"])}


@router.post("/{notification_id}/read")
async def read_one(notification_id: str, user: CurrentUser):
    if not await svc.mark_read(user["_id"], notification_id):
        # Already read, or not this user's — same answer either way.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notification not found")
    return {"status": "ok", "unread": await svc.unread_count(user["_id"])}


@router.post("/read-all")
async def read_all(user: CurrentUser):
    return {"status": "ok", "marked": await svc.mark_all_read(user["_id"]), "unread": 0}


@router.delete("/{notification_id}")
async def delete_one(notification_id: str, user: CurrentUser):
    if not await svc.delete_notification(user["_id"], notification_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notification not found")
    return {"status": "ok", "unread": await svc.unread_count(user["_id"])}
