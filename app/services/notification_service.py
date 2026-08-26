"""In-app notifications.

Deliberately fire-and-forget: `notify()` never raises and never blocks the
action that triggered it. A booking is still booked if its notification row
fails to write.

Kinds are a closed set so the frontend can map each to an icon and colour
without guessing.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from app.db.mongo import get_db
from app.db.repository import new_id, utcnow

log = logging.getLogger("criclab.notifications")

Kind = Literal["account", "security", "support", "coaching", "analysis", "system"]
KINDS: tuple[str, ...] = ("account", "security", "support", "coaching", "analysis", "system")


async def notify(
    *,
    user_id: str,
    kind: Kind | str,
    title: str,
    body: str = "",
    link: str | None = None,
    meta: dict[str, Any] | None = None,
) -> str | None:
    """Record one notification. Returns its id, or None if it could not be stored."""
    if kind not in KINDS:
        kind = "system"
    doc = {
        "_id": new_id("ntf"),
        "user_id": user_id,
        "kind": kind,
        "title": title[:140],
        "body": body[:400],
        "link": link,
        "meta": meta or {},
        "read_at": None,
        "created_at": utcnow(),
    }
    try:
        await get_db().notifications.insert_one(doc)
        return doc["_id"]
    except Exception as exc:
        log.warning("notification not stored for %s: %s", user_id, type(exc).__name__)
        return None


async def notify_many(user_ids: list[str], **kw: Any) -> int:
    """Same notification to several people — used for system announcements."""
    sent = 0
    for uid in user_ids:
        if await notify(user_id=uid, **kw):
            sent += 1
    return sent


async def list_for_user(
    user_id: str, *, limit: int = 20, before: str | None = None, unread_only: bool = False
) -> list[dict[str, Any]]:
    """Newest first, keyset-paginated.

    Paged on `created_at` rather than `skip`, so page 50 costs the same as page
    1 — `skip` makes the database walk everything it is skipping over.
    """
    query: dict[str, Any] = {"user_id": user_id}
    if unread_only:
        query["read_at"] = None
    if before:
        anchor = await get_db().notifications.find_one({"_id": before}, {"created_at": 1})
        if anchor:
            query["created_at"] = {"$lt": anchor["created_at"]}
    cursor = get_db().notifications.find(query).sort("created_at", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def unread_count(user_id: str) -> int:
    """Cheap: served entirely by the (user_id, read_at) index."""
    return int(await get_db().notifications.count_documents({"user_id": user_id, "read_at": None}))


async def mark_read(user_id: str, notification_id: str) -> bool:
    result = await get_db().notifications.update_one(
        # user_id in the filter is the ownership check — one query, not two.
        {"_id": notification_id, "user_id": user_id, "read_at": None},
        {"$set": {"read_at": utcnow()}},
    )
    return result.modified_count > 0


async def mark_all_read(user_id: str) -> int:
    result = await get_db().notifications.update_many(
        {"user_id": user_id, "read_at": None}, {"$set": {"read_at": utcnow()}}
    )
    return int(result.modified_count)


async def delete_notification(user_id: str, notification_id: str) -> bool:
    result = await get_db().notifications.delete_one({"_id": notification_id, "user_id": user_id})
    return result.deleted_count > 0


def public_notification(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": doc["_id"],
        "kind": doc.get("kind", "system"),
        "title": doc.get("title"),
        "body": doc.get("body"),
        "link": doc.get("link"),
        "read": doc.get("read_at") is not None,
        "created_at": doc.get("created_at"),
    }
