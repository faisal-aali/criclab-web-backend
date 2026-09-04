"""Coaching session booking endpoints.

Availability is read-only and open to anyone browsing — a visitor should be able
to see when a coach is free before they have an account. Everything that holds a
slot requires a verified account, because it sends mail on the user's behalf.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.deps import AdminUser, CurrentUser, VerifiedUser, enforce_rate_limit
from app.services import booking_service as svc
from app.services import email_service, notification_service

log = logging.getLogger("criclab.bookings")
router = APIRouter(prefix="/coaching", tags=["coaching"])

NO_COACH = "We could not find that coach"
NO_BOOKING = "We could not find that session"


class BookIn(BaseModel):
    coach: str = Field(min_length=1, max_length=60, description="Coach id or slug")
    starts_at: datetime
    session_type: str | None = Field(default=None, max_length=40)
    focus: str = Field(default="", max_length=200)
    notes: str = Field(default="", max_length=2000)
    delivery_ref: str = Field(default="", max_length=80)


class RescheduleIn(BaseModel):
    starts_at: datetime


class CancelIn(BaseModel):
    reason: str = Field(default="", max_length=300)


def _as_utc(value: datetime) -> datetime:
    """A client that sends a naive time means UTC; anything else is ambiguous."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _load_coach(reference: str) -> dict[str, Any]:
    coach = await svc.get_coach(reference)
    if not coach:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_COACH)
    return coach


# --------------------------------------------------------------------------- #
# Browsing
# --------------------------------------------------------------------------- #

@router.get("/coaches")
async def list_coaches():
    coaches = await svc.list_coaches()
    return {"items": [svc.public_coach(c) for c in coaches]}


@router.get("/coaches/{reference}")
async def read_coach(reference: str):
    coach = await _load_coach(reference)
    if not coach.get("active"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_COACH)
    return {"coach": svc.public_coach(coach, detailed=True)}


@router.get("/coaches/{reference}/availability")
async def coach_availability(
    reference: str,
    session_type: str | None = Query(None, max_length=40),
    days: int = Query(14, ge=1, le=60),
):
    coach = await _load_coach(reference)
    try:
        return await svc.available_slots(coach, session_type_id=session_type, days=days)
    except svc.BookingError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


# --------------------------------------------------------------------------- #
# The user's sessions
# --------------------------------------------------------------------------- #

@router.get("/bookings")
async def my_bookings(user: CurrentUser, upcoming: bool | None = Query(None)):
    # Sweeping here keeps finished sessions out of the upcoming list without a
    # scheduled job; it only ever touches rows that are already in the past.
    await svc.complete_past_sessions()
    rows = await svc.list_bookings(user["_id"], upcoming=upcoming)
    return {"items": [svc.public_booking(b) for b in rows]}


@router.post("/bookings", status_code=status.HTTP_201_CREATED)
async def book_session(payload: BookIn, user: VerifiedUser, background: BackgroundTasks):
    await enforce_rate_limit(
        f"coaching:book:{user['_id']}",
        limit=15,
        window_seconds=3600,
        message="Too many booking attempts just now. Try again shortly.",
    )
    coach = await _load_coach(payload.coach)
    try:
        booking = await svc.create_booking(
            user=user,
            coach=coach,
            starts_at=_as_utc(payload.starts_at),
            session_type_id=payload.session_type,
            focus=payload.focus,
            notes=payload.notes,
            delivery_ref=payload.delivery_ref,
        )
    except svc.BookingError as exc:
        # 409, not 400: the request was well formed, the world moved.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    when = svc.format_when(booking)
    await notification_service.notify(
        user_id=user["_id"],
        kind="coaching",
        title=f"Session booked with {booking['coach_name']}",
        body=when,
        link="/app/coaching",
        meta={"booking_id": booking["_id"]},
    )
    background.add_task(
        email_service.send_booking_confirmed,
        user["email"], user.get("name", ""), booking["coach_name"], when,
        f"{booking['minutes']} minutes", booking.get("focus") or booking["session_label"],
    )
    return {"booking": svc.public_booking(booking)}


@router.get("/bookings/{booking_id}")
async def read_booking(booking_id: str, user: CurrentUser):
    booking = await svc.get_booking(booking_id, user_id=user["_id"])
    if not booking:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_BOOKING)
    return {"booking": svc.public_booking(booking)}


@router.post("/bookings/{booking_id}/cancel")
async def cancel_session(
    booking_id: str, payload: CancelIn, user: CurrentUser, background: BackgroundTasks
):
    booking = await svc.get_booking(booking_id, user_id=user["_id"])
    if not booking:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_BOOKING)
    try:
        cancelled = await svc.cancel_booking(booking, reason=payload.reason)
    except svc.BookingError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    when = svc.format_when(cancelled)
    await notification_service.notify(
        user_id=user["_id"],
        kind="coaching",
        title="Session cancelled",
        body=f"{cancelled['coach_name']} — {when}",
        link="/app/coaching",
    )
    background.add_task(
        email_service.send_booking_cancelled,
        user["email"], user.get("name", ""), cancelled["coach_name"], when, payload.reason,
    )
    return {"booking": svc.public_booking(cancelled)}


@router.post("/bookings/{booking_id}/reschedule")
async def reschedule_session(
    booking_id: str, payload: RescheduleIn, user: CurrentUser, background: BackgroundTasks
):
    booking = await svc.get_booking(booking_id, user_id=user["_id"])
    if not booking:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_BOOKING)
    coach = await svc.get_coach(booking["coach_id"])
    if not coach:
        raise HTTPException(status.HTTP_409_CONFLICT, "That coach is no longer taking sessions")

    was = svc.format_when(booking)
    try:
        moved = await svc.reschedule_booking(
            booking=booking, coach=coach, starts_at=_as_utc(payload.starts_at)
        )
    except svc.BookingError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    now_at = svc.format_when(moved)
    await notification_service.notify(
        user_id=user["_id"],
        kind="coaching",
        title="Session moved",
        body=f"{moved['coach_name']} — {now_at}",
        link="/app/coaching",
        meta={"booking_id": moved["_id"]},
    )
    background.add_task(
        email_service.send_booking_rescheduled,
        user["email"], user.get("name", ""), moved["coach_name"], was, now_at,
    )
    return {"booking": svc.public_booking(moved)}


# --------------------------------------------------------------------------- #
# Administration
# --------------------------------------------------------------------------- #

@router.get("/admin/coaches")
async def admin_list_coaches(_: AdminUser):
    coaches = await svc.list_coaches(include_inactive=True)
    return {
        "items": [
            {
                **svc.public_coach(c, detailed=True),
                "active": c.get("active", True),
                "availability": c.get("availability") or [],
                "display_order": c.get("display_order", 100),
            }
            for c in coaches
        ]
    }


@router.put("/admin/coaches")
async def admin_upsert_coach(payload: dict[str, Any], _: AdminUser):
    try:
        coach = await svc.upsert_coach(payload)
    except svc.BookingError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"coach": svc.public_coach(coach, detailed=True)}


@router.post("/admin/reminders")
async def admin_send_reminders(_: AdminUser, background: BackgroundTasks):
    """Send the 24-hour reminder for every session that is due one.

    Driven by a request rather than an internal timer so it can be triggered by
    whatever scheduler is running in a given deployment, and so it is visible.
    """
    due = await svc.due_reminders()
    for booking in due:
        when = svc.format_when(booking)
        await notification_service.notify(
            user_id=booking["user_id"],
            kind="coaching",
            title="Session tomorrow",
            body=f"{booking['coach_name']} — {when}",
            link="/app/coaching",
        )
        background.add_task(
            email_service.send_booking_reminder,
            booking.get("user_email", ""), booking.get("user_name", ""),
            booking["coach_name"], when,
        )
        await svc.mark_reminded(booking["_id"])
    return {"reminded": len(due)}
