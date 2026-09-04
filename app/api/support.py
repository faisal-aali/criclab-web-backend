"""Support ticketing endpoints.

The user-facing half is scoped by construction: every handler resolves the
ticket with `user_id=user["_id"]` in the filter, so there is no code path where
a valid reference belonging to someone else can be loaded and then rejected.

The staff half sits behind `AdminUser` and is the only place tickets can be
read across accounts.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api.deps import AdminUser, CurrentUser, VerifiedUser, enforce_rate_limit
from app.db import auth_repo
from app.services import email_service, notification_service
from app.services import support_service as svc

log = logging.getLogger("criclab.support")
router = APIRouter(prefix="/support", tags=["support"])

NOT_FOUND = "We could not find that ticket"


class StatusIn(BaseModel):
    status: str = Field(min_length=1, max_length=32)


class ReplyIn(BaseModel):
    body: str = Field(min_length=1, max_length=8000)


class StaffReplyIn(ReplyIn):
    resolve: bool = False


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

async def _collect_attachments(
    ticket_id: str, files: list[UploadFile] | None
) -> list[dict[str, Any]]:
    files = [f for f in (files or []) if f and f.filename]
    if not files:
        return []
    if len(files) > svc.MAX_ATTACHMENTS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Up to {svc.MAX_ATTACHMENTS} files per message.",
        )
    stored: list[dict[str, Any]] = []
    try:
        for upload in files:
            stored.append(
                await svc.store_attachment(
                    ticket_id=ticket_id,
                    stream=upload.file,
                    filename=upload.filename or "file",
                    content_type=upload.content_type or "",
                )
            )
    except svc.AttachmentRejected as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return stored


def _thread_link(ticket_id: str) -> str:
    return f"/app/support/{ticket_id}"


# --------------------------------------------------------------------------- #
# Reference data
# --------------------------------------------------------------------------- #

@router.get("/meta")
async def support_meta():
    """Categories and priorities, so the form never hardcodes a list that drifts."""
    return {
        "categories": [
            {"value": "analysis", "label": "Analysis or metrics"},
            {"value": "technical", "label": "Something is broken"},
            {"value": "account", "label": "Account and sign-in"},
            {"value": "billing", "label": "Billing and plans"},
            {"value": "coaching", "label": "Coaching sessions"},
            {"value": "feedback", "label": "Feedback or an idea"},
            {"value": "other", "label": "Something else"},
        ],
        "priorities": [
            {"value": "low", "label": "Whenever"},
            {"value": "normal", "label": "Normal"},
            {"value": "high", "label": "Blocking my session"},
            {"value": "urgent", "label": "Urgent"},
        ],
        "max_attachments": svc.MAX_ATTACHMENTS,
        "max_attachment_mb": svc.MAX_ATTACHMENT_BYTES // (1024 * 1024),
        "accepted_types": sorted(svc.ALLOWED_ATTACHMENTS.keys()),
    }


# --------------------------------------------------------------------------- #
# The user's own tickets
# --------------------------------------------------------------------------- #

@router.get("/tickets")
async def my_tickets(
    user: CurrentUser,
    live_only: bool = Query(False),
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(20, ge=1, le=50),
    before: str | None = Query(None),
):
    rows = await svc.list_tickets(
        user_id=user["_id"],
        status=status_filter,
        live_only=live_only,
        limit=limit,
        before=before,
    )
    return {
        "items": [svc.public_ticket(t) for t in rows],
        "unread": await svc.unread_ticket_count(user["_id"]),
        "next_cursor": rows[-1]["_id"] if len(rows) == limit else None,
    }


@router.post("/tickets", status_code=status.HTTP_201_CREATED)
async def open_ticket(
    user: VerifiedUser,
    background: BackgroundTasks,
    subject: Annotated[str, Form(min_length=3, max_length=140)],
    body: Annotated[str, Form(min_length=10, max_length=8000)],
    category: Annotated[str, Form()] = "other",
    priority: Annotated[str, Form()] = "normal",
    context_page: Annotated[str, Form()] = "",
    context_ref: Annotated[str, Form()] = "",
    files: Annotated[list[UploadFile] | None, File()] = None,
):
    """Open a ticket. Multipart, because it carries the first message's files."""
    # A person with a real problem files two or three, not thirty. This is about
    # a runaway client or a script, not about rationing help.
    await enforce_rate_limit(
        f"support:create:{user['_id']}",
        limit=10,
        window_seconds=3600,
        message="That is a lot of tickets in an hour. Reply on an existing one instead.",
    )

    ticket = await svc.create_ticket(
        user=user,
        subject=subject,
        body=body,
        category=category,
        priority=priority,
        context={"page": context_page[:200], "ref": context_ref[:80]},
    )
    stored = await _collect_attachments(ticket["_id"], files)
    if stored:
        from app.db.mongo import get_db

        await get_db().ticket_messages.update_one(
            {"ticket_id": ticket["_id"]}, {"$set": {"attachments": stored}}
        )

    await notification_service.notify(
        user_id=user["_id"],
        kind="support",
        title=f"Ticket {ticket['_id']} opened",
        body=ticket["subject"],
        link=_thread_link(ticket["_id"]),
    )
    # Mail is confirmation, not part of the transaction.
    background.add_task(
        email_service.send_ticket_created,
        user["email"], user.get("name", ""), ticket["_id"], ticket["subject"],
    )
    return {"ticket": svc.public_ticket(ticket)}


@router.get("/tickets/{ticket_id}")
async def read_ticket(ticket_id: str, user: CurrentUser):
    ticket = await svc.get_ticket(ticket_id, user_id=user["_id"])
    if not ticket:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    messages = await svc.list_messages(ticket_id)
    # Opening the thread is what clears the badge.
    await svc.mark_thread_seen(ticket_id, by_staff=False)
    ticket["unread_for_user"] = 0
    return {
        "ticket": svc.public_ticket(ticket),
        "messages": [svc.public_message(m) for m in messages],
    }


@router.post("/tickets/{ticket_id}/reply")
async def reply_to_ticket(
    ticket_id: str,
    user: CurrentUser,
    body: Annotated[str, Form(min_length=1, max_length=8000)],
    files: Annotated[list[UploadFile] | None, File()] = None,
):
    ticket = await svc.get_ticket(ticket_id, user_id=user["_id"])
    if not ticket:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    if ticket.get("status") == svc.CLOSED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This ticket is closed. Open a new one and we will pick it up from here.",
        )
    await enforce_rate_limit(
        f"support:reply:{user['_id']}",
        limit=60,
        window_seconds=3600,
        message="Slow down a moment — try that again shortly.",
    )
    stored = await _collect_attachments(ticket_id, files)
    message = await svc.add_message(
        ticket=ticket, author=user, body=body, from_staff=False, attachments=stored
    )
    updated = await svc.get_ticket(ticket_id, user_id=user["_id"])
    return {
        "message": svc.public_message(message),
        "ticket": svc.public_ticket(updated or ticket),
    }


@router.post("/tickets/{ticket_id}/status")
async def change_my_ticket_status(ticket_id: str, payload: StatusIn, user: CurrentUser):
    """A user may resolve or reopen their own ticket — nothing else."""
    wanted = payload.status
    if wanted not in (svc.RESOLVED, svc.AWAITING_SUPPORT):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That is not a change you can make")
    ticket = await svc.get_ticket(ticket_id, user_id=user["_id"])
    if not ticket:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    if ticket.get("status") == svc.CLOSED:
        raise HTTPException(status.HTTP_409_CONFLICT, "This ticket is closed")
    updated = await svc.set_status(ticket_id, wanted, user_id=user["_id"])
    return {"ticket": svc.public_ticket(updated or ticket)}


@router.get("/tickets/{ticket_id}/attachments/{attachment_id}")
async def download_attachment(ticket_id: str, attachment_id: str, user: CurrentUser):
    """Serve a file only to the ticket's owner, or to staff."""
    ticket = await svc.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    if ticket.get("user_id") != user["_id"] and user.get("role") != "admin":
        # Same answer as a missing ticket: no signal that it exists.
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    attachment = await svc.find_attachment(ticket_id, attachment_id)
    if not attachment:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That file is no longer available")
    return FileResponse(
        attachment["path"],
        media_type=attachment.get("content_type") or "application/octet-stream",
        filename=attachment.get("name") or "attachment",
        # Never rendered inline on this origin, whatever the type claims to be.
        headers={"X-Content-Type-Options": "nosniff"},
    )


# --------------------------------------------------------------------------- #
# Staff queue
# --------------------------------------------------------------------------- #

@router.get("/queue")
async def staff_queue(
    _: AdminUser,
    status_filter: str | None = Query(None, alias="status"),
    live_only: bool = Query(True),
    limit: int = Query(25, ge=1, le=50),
    before: str | None = Query(None),
):
    rows = await svc.list_tickets(
        status=status_filter, live_only=live_only, limit=limit, before=before
    )
    return {
        "items": [svc.public_ticket(t, for_staff=True) for t in rows],
        "counts": await svc.queue_summary(),
        "next_cursor": rows[-1]["_id"] if len(rows) == limit else None,
    }


@router.get("/queue/{ticket_id}")
async def staff_read_ticket(ticket_id: str, _: AdminUser):
    ticket = await svc.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    messages = await svc.list_messages(ticket_id)
    await svc.mark_thread_seen(ticket_id, by_staff=True)
    ticket["unread_for_staff"] = 0
    return {
        "ticket": svc.public_ticket(ticket, for_staff=True),
        "messages": [svc.public_message(m) for m in messages],
        "context": ticket.get("context") or {},
    }


@router.post("/queue/{ticket_id}/reply")
async def staff_reply(
    ticket_id: str,
    payload: StaffReplyIn,
    staff: AdminUser,
    background: BackgroundTasks,
):
    ticket = await svc.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)

    author = dict(staff)
    author["name"] = "CricLab Support"
    await svc.add_message(ticket=ticket, author=author, body=payload.body, from_staff=True)
    if payload.resolve:
        await svc.set_status(ticket_id, svc.RESOLVED)

    owner = await auth_repo.get_user(ticket["user_id"])
    if owner:
        await notification_service.notify(
            user_id=owner["_id"],
            kind="support",
            title=f"Reply on {ticket['_id']}",
            body=ticket.get("subject", ""),
            link=_thread_link(ticket_id),
        )
        sender = (
            email_service.send_ticket_resolved
            if payload.resolve
            else email_service.send_ticket_updated
        )
        args = [owner["email"], owner.get("name", ""), ticket_id, ticket.get("subject", "")]
        if not payload.resolve:
            args.append("Awaiting your reply")
        background.add_task(sender, *args)

    updated = await svc.get_ticket(ticket_id)
    return {"ticket": svc.public_ticket(updated or ticket, for_staff=True)}


@router.post("/queue/{ticket_id}/status")
async def staff_set_status(ticket_id: str, payload: StatusIn, _: AdminUser):
    updated = await svc.set_status(ticket_id, payload.status)
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return {"ticket": svc.public_ticket(updated, for_staff=True)}
