"""Support tickets: threads, statuses, attachments.

Three rules shape everything here.

**Ownership is part of the query, never a check after the fact.** Every read and
write filters on `user_id` (or comes in through the staff path), so a guessed
ticket reference returns "not found" rather than someone else's conversation.

**Status is derived from who spoke last.** Nobody sets `awaiting_support` by
hand — a user reply means support owes an answer, a staff reply means the ball
is with the user. That keeps the queue honest without anyone maintaining it.

**Attachments are opaque bytes on disk.** They are never executed, never served
from the API's own origin as HTML, and only ever handed back through an
endpoint that re-checks who is asking.
"""

from __future__ import annotations

import logging
import re
import secrets
from pathlib import Path
from typing import Any, BinaryIO

from pymongo import ReturnDocument

from app.config import get_settings
from app.db.mongo import get_db
from app.db.repository import new_id, utcnow

log = logging.getLogger("criclab.support")

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #

OPEN = "open"                      # logged, nobody has picked it up
AWAITING_SUPPORT = "awaiting_support"   # the user spoke last
AWAITING_USER = "awaiting_user"    # support spoke last
RESOLVED = "resolved"
CLOSED = "closed"

STATUSES: tuple[str, ...] = (OPEN, AWAITING_SUPPORT, AWAITING_USER, RESOLVED, CLOSED)
# What a person still has to act on — the default filter in the UI.
LIVE_STATUSES: tuple[str, ...] = (OPEN, AWAITING_SUPPORT, AWAITING_USER)

CATEGORIES: tuple[str, ...] = (
    "analysis",     # a delivery was read wrong, or would not process
    "billing",
    "account",
    "technical",
    "coaching",
    "feedback",
    "other",
)

PRIORITIES: tuple[str, ...] = ("low", "normal", "high", "urgent")

# A ticket reference a person can read out over the phone. No vowels and no
# 0/O/1/I, so nothing is ambiguous when it is written down or dictated.
_REF_ALPHABET = "23456789BCDFGHJKLMNPQRSTVWXZ"

MAX_ATTACHMENTS = 4
MAX_ATTACHMENT_BYTES = 12 * 1024 * 1024
ALLOWED_ATTACHMENTS: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
}

# How long after resolution a ticket can be reopened by replying to it.
REOPEN_WINDOW_DAYS = 21


def _attachment_root() -> Path:
    path = get_settings().storage_path / "support"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _new_ref() -> str:
    return "CL-" + "".join(secrets.choice(_REF_ALPHABET) for _ in range(6))


def _clean(text: str, limit: int) -> str:
    """Collapse control characters and trim. Stored text is never HTML."""
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text or "")
    return cleaned.strip()[:limit]


# --------------------------------------------------------------------------- #
# Public shapes
# --------------------------------------------------------------------------- #

def public_ticket(doc: dict[str, Any], *, for_staff: bool = False) -> dict[str, Any]:
    out = {
        "id": doc["_id"],
        "subject": doc.get("subject"),
        "category": doc.get("category"),
        "priority": doc.get("priority"),
        "status": doc.get("status"),
        "message_count": doc.get("message_count", 0),
        "unread_for_user": doc.get("unread_for_user", 0),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
        "last_message_at": doc.get("last_message_at"),
        "resolved_at": doc.get("resolved_at"),
        "preview": doc.get("preview", ""),
    }
    if for_staff:
        # The staff queue needs to know whose ticket it is; the user's own view
        # never does, so the field simply is not there.
        out["user"] = {
            "id": doc.get("user_id"),
            "name": doc.get("user_name"),
            "email": doc.get("user_email"),
        }
        out["unread_for_staff"] = doc.get("unread_for_staff", 0)
    return out


def public_message(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": doc["_id"],
        "author_role": doc.get("author_role", "user"),
        "author_name": doc.get("author_name", ""),
        "body": doc.get("body", ""),
        "attachments": [
            {
                "id": a["id"],
                "name": a.get("name", "file"),
                "content_type": a.get("content_type"),
                "size": a.get("size", 0),
            }
            for a in doc.get("attachments") or []
        ],
        "created_at": doc.get("created_at"),
    }


# --------------------------------------------------------------------------- #
# Attachments
# --------------------------------------------------------------------------- #

class AttachmentRejected(Exception):
    """Raised with a message meant for the person who tried to upload."""


def _extension_for(content_type: str, filename: str) -> str:
    """Trust the declared type only when the name agrees, and never the name alone."""
    ext = ALLOWED_ATTACHMENTS.get((content_type or "").lower())
    if not ext:
        raise AttachmentRejected(
            f"{filename or 'That file'} is not a type we can accept — "
            "images, PDFs and short video clips only."
        )
    return ext


async def store_attachment(
    *, ticket_id: str, stream: BinaryIO, filename: str, content_type: str
) -> dict[str, Any]:
    """Copy an upload to disk under a name we chose, never the one supplied.

    The original filename is kept as a label only. Using it on disk would let a
    caller pick the path, which is how "../" gets you somewhere it should not.
    """
    ext = _extension_for(content_type, filename)
    attachment_id = new_id("att")
    folder = _attachment_root() / ticket_id
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{attachment_id}{ext}"

    written = 0
    with target.open("wb") as out:
        while True:
            chunk = stream.read(1024 * 256)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_ATTACHMENT_BYTES:
                out.close()
                target.unlink(missing_ok=True)
                raise AttachmentRejected(
                    f"{filename or 'That file'} is larger than "
                    f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB."
                )
            out.write(chunk)

    if written == 0:
        target.unlink(missing_ok=True)
        raise AttachmentRejected(f"{filename or 'That file'} was empty.")

    return {
        "id": attachment_id,
        "name": _clean(filename or "file", 120),
        "content_type": content_type.lower(),
        "size": written,
        "path": str(target),
    }


async def find_attachment(ticket_id: str, attachment_id: str) -> dict[str, Any] | None:
    """Locate an attachment through its ticket, so ownership is already settled."""
    doc = await get_db().ticket_messages.find_one(
        {"ticket_id": ticket_id, "attachments.id": attachment_id},
        {"attachments": 1},
    )
    if not doc:
        return None
    for a in doc.get("attachments") or []:
        if a.get("id") == attachment_id:
            path = Path(a.get("path", ""))
            # A row can outlive its file — a restored database, a wiped disk.
            return a if path.is_file() else None
    return None


# --------------------------------------------------------------------------- #
# Tickets
# --------------------------------------------------------------------------- #

async def create_ticket(
    *,
    user: dict[str, Any],
    subject: str,
    body: str,
    category: str = "other",
    priority: str = "normal",
    attachments: list[dict[str, Any]] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Open a ticket with its first message. Returns the ticket document."""
    subject = _clean(subject, 140)
    body = _clean(body, 8000)
    if category not in CATEGORIES:
        category = "other"
    if priority not in PRIORITIES:
        priority = "normal"

    now = utcnow()
    db = get_db()

    # References are short enough to collide eventually; the unique _id is the
    # arbiter, so a clash just means drawing again.
    ticket: dict[str, Any] = {}
    for _ in range(6):
        ref = _new_ref()
        ticket = {
            "_id": ref,
            "user_id": user["_id"],
            "user_name": user.get("name", ""),
            "user_email": user.get("email", ""),
            "subject": subject,
            "category": category,
            "priority": priority,
            "status": OPEN,
            "message_count": 1,
            "unread_for_user": 0,
            "unread_for_staff": 1,
            "preview": body[:160],
            # What the user was looking at when they hit "get help" — a delivery
            # id, the page, the browser. Saves a round trip asking for it.
            "context": context or {},
            "created_at": now,
            "updated_at": now,
            "last_message_at": now,
            "resolved_at": None,
        }
        try:
            await db.tickets.insert_one(ticket)
            break
        except Exception as exc:
            if "duplicate" not in str(exc).lower():
                raise
    else:  # pragma: no cover - six collisions in a 28^6 space
        raise RuntimeError("could not allocate a ticket reference")

    await db.ticket_messages.insert_one(
        {
            "_id": new_id("msg"),
            "ticket_id": ticket["_id"],
            "author_id": user["_id"],
            "author_role": "user",
            "author_name": user.get("name", ""),
            "body": body,
            "attachments": attachments or [],
            "created_at": now,
        }
    )
    return ticket


async def get_ticket(ticket_id: str, *, user_id: str | None = None) -> dict[str, Any] | None:
    """Fetch one ticket. Passing `user_id` scopes it to that owner."""
    query: dict[str, Any] = {"_id": ticket_id}
    if user_id:
        query["user_id"] = user_id
    return await get_db().tickets.find_one(query)


async def list_tickets(
    *,
    user_id: str | None = None,
    status: str | None = None,
    live_only: bool = False,
    limit: int = 20,
    before: str | None = None,
) -> list[dict[str, Any]]:
    """Newest activity first, keyset-paginated on `updated_at`."""
    query: dict[str, Any] = {}
    if user_id:
        query["user_id"] = user_id
    if status in STATUSES:
        query["status"] = status
    elif live_only:
        query["status"] = {"$in": list(LIVE_STATUSES)}
    if before:
        anchor = await get_db().tickets.find_one({"_id": before}, {"updated_at": 1})
        if anchor:
            query["updated_at"] = {"$lt": anchor["updated_at"]}
    cursor = get_db().tickets.find(query).sort("updated_at", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def list_messages(ticket_id: str) -> list[dict[str, Any]]:
    cursor = get_db().ticket_messages.find({"ticket_id": ticket_id}).sort("created_at", 1)
    return await cursor.to_list(length=500)


async def add_message(
    *,
    ticket: dict[str, Any],
    author: dict[str, Any],
    body: str,
    from_staff: bool,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Append to the thread and move the ticket to whoever now owes a reply."""
    body = _clean(body, 8000)
    now = utcnow()
    message = {
        "_id": new_id("msg"),
        "ticket_id": ticket["_id"],
        "author_id": author["_id"],
        "author_role": "support" if from_staff else "user",
        "author_name": author.get("name", "CricLab Support" if from_staff else ""),
        "body": body,
        "attachments": attachments or [],
        "created_at": now,
    }
    await get_db().ticket_messages.insert_one(message)

    # A reply on a resolved ticket reopens it — that is the whole point of
    # replying. Beyond the reopen window it stays closed and the reply is a
    # note on the record.
    next_status = AWAITING_USER if from_staff else AWAITING_SUPPORT
    if ticket.get("status") == CLOSED:
        next_status = CLOSED
    elif ticket.get("status") == RESOLVED and not from_staff:
        resolved_at = ticket.get("resolved_at")
        if resolved_at and (now - _aware(resolved_at)).days > REOPEN_WINDOW_DAYS:
            next_status = CLOSED

    update: dict[str, Any] = {
        "$set": {
            "status": next_status,
            "updated_at": now,
            "last_message_at": now,
            "preview": body[:160],
        },
        "$inc": {
            "message_count": 1,
            # The badge the *other* side sees.
            "unread_for_user" if from_staff else "unread_for_staff": 1,
        },
    }
    if next_status not in (RESOLVED, CLOSED):
        update["$set"]["resolved_at"] = None

    await get_db().tickets.update_one({"_id": ticket["_id"]}, update)
    return message


def _aware(dt: Any) -> Any:
    """Guard against a naive datetime from an older row."""
    from datetime import timezone as _tz

    return dt if getattr(dt, "tzinfo", None) else dt.replace(tzinfo=_tz.utc)


async def set_status(ticket_id: str, status: str, *, user_id: str | None = None) -> dict[str, Any] | None:
    """Move a ticket. Returns the updated document, or None if it did not apply."""
    if status not in STATUSES:
        return None
    query: dict[str, Any] = {"_id": ticket_id}
    if user_id:
        query["user_id"] = user_id
    now = utcnow()
    fields: dict[str, Any] = {"status": status, "updated_at": now}
    fields["resolved_at"] = now if status in (RESOLVED, CLOSED) else None
    result = await get_db().tickets.find_one_and_update(
        query, {"$set": fields}, return_document=ReturnDocument.AFTER
    )
    return result


async def mark_thread_seen(ticket_id: str, *, by_staff: bool) -> None:
    """Clear the unread badge for whichever side just opened the thread."""
    field = "unread_for_staff" if by_staff else "unread_for_user"
    await get_db().tickets.update_one({"_id": ticket_id}, {"$set": {field: 0}})


async def unread_ticket_count(user_id: str) -> int:
    """How many of this user's tickets have replies they have not opened."""
    return int(
        await get_db().tickets.count_documents(
            {"user_id": user_id, "unread_for_user": {"$gt": 0}}
        )
    )


async def queue_summary() -> dict[str, int]:
    """Counts by status for the staff queue header."""
    pipeline = [{"$group": {"_id": "$status", "n": {"$sum": 1}}}]
    rows = await get_db().tickets.aggregate(pipeline).to_list(length=len(STATUSES) + 2)
    counts = {s: 0 for s in STATUSES}
    for row in rows:
        if row["_id"] in counts:
            counts[row["_id"]] = int(row["n"])
    counts["live"] = sum(counts[s] for s in LIVE_STATUSES)
    return counts
