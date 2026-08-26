"""Coaching sessions: availability, slot locking, cancellation and reschedule.

Availability is stored as intent — weekly windows in the coach's own timezone —
and slots are computed on demand. Materialising every slot into rows would mean
a nightly job to extend the horizon and a migration every time a coach changes
their Tuesdays.

The one thing that is *not* computed is whether a slot is taken. Two people
pressing "book" on the last Tuesday slot at the same moment both see it free;
what stops the double booking is a unique index on (coach, start time), so the
database rejects the second write. Checking first and inserting after would
leave a window between the two.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pymongo import ReturnDocument

from app.db.mongo import get_db
from app.db.repository import new_id, utcnow

log = logging.getLogger("criclab.bookings")

PENDING = "pending"
CONFIRMED = "confirmed"
CANCELLED = "cancelled"
COMPLETED = "completed"
# These are the two states the unique slot index treats as "taken".
HOLDS_SLOT: tuple[str, ...] = (PENDING, CONFIRMED)

DEFAULT_TIMEZONE = "Asia/Karachi"
DEFAULT_SLOT_STEP = 30          # minutes between offered start times
DEFAULT_LEAD_HOURS = 12         # no booking closer than this
DEFAULT_HORIZON_DAYS = 28       # how far ahead the calendar opens
DEFAULT_CANCEL_WINDOW = 12      # free cancellation up to this many hours before
MAX_UPCOMING_PER_USER = 6       # a fair-use ceiling, not a plan limit

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class BookingError(Exception):
    """Carries a message meant to be shown to the person booking."""


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _aware(dt: datetime | None) -> datetime | None:
    """Older rows may be naive; BSON stores UTC either way."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_hhmm(value: str) -> time | None:
    match = _TIME_RE.match((value or "").strip())
    if not match:
        return None
    return time(int(match.group(1)), int(match.group(2)))


# --------------------------------------------------------------------------- #
# Public shapes
# --------------------------------------------------------------------------- #

def public_coach(doc: dict[str, Any], *, detailed: bool = False) -> dict[str, Any]:
    out = {
        "id": doc["_id"],
        "slug": doc.get("slug"),
        "name": doc.get("name"),
        "title": doc.get("title"),
        "headline": doc.get("headline", ""),
        "specialities": doc.get("specialities") or [],
        "languages": doc.get("languages") or [],
        "accent": doc.get("accent", "lime"),
        "initials": doc.get("initials") or "".join(p[:1] for p in (doc.get("name") or "C").split()[:2]).upper(),
        "timezone": doc.get("timezone", DEFAULT_TIMEZONE),
        "session_types": [
            {
                "id": s.get("id"),
                "label": s.get("label"),
                "minutes": s.get("minutes"),
                "description": s.get("description", ""),
            }
            for s in doc.get("session_types") or []
        ],
    }
    if detailed:
        out["bio"] = doc.get("bio", "")
        out["cancel_window_hours"] = doc.get("cancel_window_hours", DEFAULT_CANCEL_WINDOW)
        out["lead_time_hours"] = doc.get("lead_time_hours", DEFAULT_LEAD_HOURS)
        out["horizon_days"] = doc.get("horizon_days", DEFAULT_HORIZON_DAYS)
    return out


def public_booking(doc: dict[str, Any]) -> dict[str, Any]:
    starts = _aware(doc.get("starts_at"))
    window = int(doc.get("cancel_window_hours", DEFAULT_CANCEL_WINDOW))
    return {
        "id": doc["_id"],
        "coach": {"id": doc.get("coach_id"), "name": doc.get("coach_name"), "slug": doc.get("coach_slug")},
        "session_type": doc.get("session_type"),
        "session_label": doc.get("session_label"),
        "minutes": doc.get("minutes"),
        "starts_at": starts,
        "ends_at": _aware(doc.get("ends_at")),
        "timezone": doc.get("timezone", DEFAULT_TIMEZONE),
        "status": doc.get("status"),
        "focus": doc.get("focus", ""),
        "notes": doc.get("notes", ""),
        "delivery_ref": doc.get("delivery_ref") or None,
        "created_at": _aware(doc.get("created_at")),
        "cancel_reason": doc.get("cancel_reason") or None,
        # Computed here so the UI never has to reimplement the rule.
        "can_cancel": _can_change(doc, window),
        "can_reschedule": _can_change(doc, window),
        "cancel_window_hours": window,
    }


def _can_change(doc: dict[str, Any], window_hours: int) -> bool:
    if doc.get("status") not in HOLDS_SLOT:
        return False
    starts = _aware(doc.get("starts_at"))
    if not starts:
        return False
    return starts - utcnow() > timedelta(hours=window_hours)


# --------------------------------------------------------------------------- #
# Coaches
# --------------------------------------------------------------------------- #

async def list_coaches(*, include_inactive: bool = False) -> list[dict[str, Any]]:
    query: dict[str, Any] = {} if include_inactive else {"active": True}
    cursor = get_db().coaches.find(query).sort("display_order", 1)
    return await cursor.to_list(length=100)


async def get_coach(coach_id_or_slug: str) -> dict[str, Any] | None:
    return await get_db().coaches.find_one(
        {"$or": [{"_id": coach_id_or_slug}, {"slug": coach_id_or_slug}]}
    )


async def upsert_coach(payload: dict[str, Any]) -> dict[str, Any]:
    """Create or replace a coach profile. Availability is validated, not trusted."""
    slug = (payload.get("slug") or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", slug):
        raise BookingError("A coach needs a URL-safe slug, such as 'asif-mehmood'.")

    windows: list[dict[str, Any]] = []
    for w in payload.get("availability") or []:
        start = _parse_hhmm(str(w.get("start", "")))
        end = _parse_hhmm(str(w.get("end", "")))
        weekday = w.get("weekday")
        if start is None or end is None or not isinstance(weekday, int) or not 0 <= weekday <= 6:
            raise BookingError("Availability needs a weekday 0-6 and 24-hour HH:MM times.")
        if end <= start:
            raise BookingError("An availability window has to end after it starts.")
        windows.append({"weekday": weekday, "start": w["start"], "end": w["end"]})

    existing = await get_db().coaches.find_one({"slug": slug})
    doc = {
        "slug": slug,
        "name": (payload.get("name") or "").strip()[:80],
        "title": (payload.get("title") or "").strip()[:120],
        "headline": (payload.get("headline") or "").strip()[:200],
        "bio": (payload.get("bio") or "").strip()[:2000],
        "specialities": [str(s)[:40] for s in (payload.get("specialities") or [])][:8],
        "languages": [str(s)[:30] for s in (payload.get("languages") or [])][:6],
        "accent": payload.get("accent") or "lime",
        "timezone": payload.get("timezone") or DEFAULT_TIMEZONE,
        "session_types": payload.get("session_types") or [],
        "availability": windows,
        "blackouts": payload.get("blackouts") or [],
        "slot_step_minutes": int(payload.get("slot_step_minutes") or DEFAULT_SLOT_STEP),
        "lead_time_hours": int(payload.get("lead_time_hours") or DEFAULT_LEAD_HOURS),
        "horizon_days": int(payload.get("horizon_days") or DEFAULT_HORIZON_DAYS),
        "cancel_window_hours": int(payload.get("cancel_window_hours") or DEFAULT_CANCEL_WINDOW),
        "active": bool(payload.get("active", True)),
        "display_order": int(payload.get("display_order") or 100),
        "updated_at": utcnow(),
    }
    if existing:
        await get_db().coaches.update_one({"_id": existing["_id"]}, {"$set": doc})
        return {**existing, **doc}
    doc["_id"] = new_id("coach")
    doc["created_at"] = utcnow()
    await get_db().coaches.insert_one(doc)
    return doc


# --------------------------------------------------------------------------- #
# Availability
# --------------------------------------------------------------------------- #

def _session_type(coach: dict[str, Any], session_type_id: str | None) -> dict[str, Any]:
    types = coach.get("session_types") or []
    if not types:
        raise BookingError("This coach has no session types set up yet.")
    if session_type_id:
        for t in types:
            if t.get("id") == session_type_id:
                return t
        raise BookingError("That session length is not offered by this coach.")
    return types[0]


def _blackout_ranges(coach: dict[str, Any]) -> list[tuple[datetime, datetime]]:
    ranges = []
    for b in coach.get("blackouts") or []:
        start, end = _aware(b.get("starts_at")), _aware(b.get("ends_at"))
        if start and end and end > start:
            ranges.append((start, end))
    return ranges


async def available_slots(
    coach: dict[str, Any],
    *,
    session_type_id: str | None = None,
    days: int | None = None,
) -> dict[str, Any]:
    """Offered start times, grouped by local day.

    A slot survives four filters: it sits inside a weekly window, it finishes
    inside that window, it is not inside a blackout, and it is not already held
    by a live booking.
    """
    session = _session_type(coach, session_type_id)
    minutes = int(session.get("minutes") or 45)
    tz = _zone(coach.get("timezone"))
    step = max(15, int(coach.get("slot_step_minutes") or DEFAULT_SLOT_STEP))
    lead = timedelta(hours=int(coach.get("lead_time_hours") or DEFAULT_LEAD_HOURS))
    horizon = min(int(days or coach.get("horizon_days") or DEFAULT_HORIZON_DAYS), 60)

    now = utcnow()
    earliest = now + lead
    last_day = (now.astimezone(tz).date()) + timedelta(days=horizon)

    windows: dict[int, list[tuple[time, time]]] = {}
    for w in coach.get("availability") or []:
        start, end = _parse_hhmm(w.get("start", "")), _parse_hhmm(w.get("end", ""))
        if start and end:
            windows.setdefault(int(w["weekday"]), []).append((start, end))
    if not windows:
        return {"session": session, "timezone": str(tz), "days": []}

    # One query for the whole horizon rather than one per day.
    taken = {
        _aware(b["starts_at"])
        for b in await get_db()
        .bookings.find(
            {
                "coach_id": coach["_id"],
                "status": {"$in": list(HOLDS_SLOT)},
                "starts_at": {"$gte": now - timedelta(hours=6)},
            },
            {"starts_at": 1},
        )
        .to_list(length=2000)
    }
    blackouts = _blackout_ranges(coach)

    days_out: list[dict[str, Any]] = []
    cursor_day: date = now.astimezone(tz).date()
    while cursor_day <= last_day:
        slots: list[datetime] = []
        for window_start, window_end in windows.get(cursor_day.weekday(), []):
            # Localise, then convert — building the instant in local time is what
            # makes a DST change land on the right side of the boundary.
            local_open = datetime.combine(cursor_day, window_start, tzinfo=tz)
            local_close = datetime.combine(cursor_day, window_end, tzinfo=tz)
            cursor_time = local_open
            while cursor_time + timedelta(minutes=minutes) <= local_close:
                instant = cursor_time.astimezone(timezone.utc)
                finish = instant + timedelta(minutes=minutes)
                if (
                    instant >= earliest
                    and instant not in taken
                    and not any(bs < finish and instant < be for bs, be in blackouts)
                ):
                    slots.append(instant)
                cursor_time += timedelta(minutes=step)
        if slots:
            days_out.append(
                {
                    "date": cursor_day.isoformat(),
                    "weekday": cursor_day.strftime("%a"),
                    "slots": sorted(slots),
                }
            )
        cursor_day += timedelta(days=1)

    return {
        "session": {
            "id": session.get("id"),
            "label": session.get("label"),
            "minutes": minutes,
            "description": session.get("description", ""),
        },
        "timezone": str(tz),
        "days": days_out,
    }


def _slot_is_offered(availability: dict[str, Any], starts_at: datetime) -> bool:
    """Re-derive the slot list and confirm the requested instant is on it.

    The client sends a start time; this makes sure it came from the calendar
    rather than from someone picking a convenient hour of their own.
    """
    target = starts_at.astimezone(timezone.utc).replace(microsecond=0)
    for day in availability.get("days") or []:
        for slot in day["slots"]:
            if slot.replace(microsecond=0) == target:
                return True
    return False


# --------------------------------------------------------------------------- #
# Bookings
# --------------------------------------------------------------------------- #

async def upcoming_count(user_id: str) -> int:
    return int(
        await get_db().bookings.count_documents(
            {"user_id": user_id, "status": {"$in": list(HOLDS_SLOT)}, "starts_at": {"$gte": utcnow()}}
        )
    )


async def create_booking(
    *,
    user: dict[str, Any],
    coach: dict[str, Any],
    starts_at: datetime,
    session_type_id: str | None,
    focus: str = "",
    notes: str = "",
    delivery_ref: str = "",
) -> dict[str, Any]:
    if not coach.get("active"):
        raise BookingError("This coach is not taking sessions at the moment.")
    if await upcoming_count(user["_id"]) >= MAX_UPCOMING_PER_USER:
        raise BookingError(
            f"You already have {MAX_UPCOMING_PER_USER} sessions booked. "
            "Take one of those first, or cancel one to free the slot."
        )

    availability = await available_slots(coach, session_type_id=session_type_id)
    if not _slot_is_offered(availability, starts_at):
        raise BookingError("That time is no longer free. Pick another slot.")

    session = availability["session"]
    minutes = int(session["minutes"])
    start = starts_at.astimezone(timezone.utc).replace(microsecond=0)
    doc = {
        "_id": new_id("bkg"),
        "user_id": user["_id"],
        "user_name": user.get("name", ""),
        "user_email": user.get("email", ""),
        "coach_id": coach["_id"],
        "coach_slug": coach.get("slug"),
        "coach_name": coach.get("name"),
        "session_type": session["id"],
        "session_label": session["label"],
        "minutes": minutes,
        "starts_at": start,
        "ends_at": start + timedelta(minutes=minutes),
        "timezone": coach.get("timezone", DEFAULT_TIMEZONE),
        "cancel_window_hours": int(coach.get("cancel_window_hours") or DEFAULT_CANCEL_WINDOW),
        "status": CONFIRMED,
        "focus": (focus or "").strip()[:200],
        "notes": (notes or "").strip()[:2000],
        "delivery_ref": (delivery_ref or "").strip()[:80],
        "created_at": utcnow(),
        "updated_at": utcnow(),
        "cancelled_at": None,
        "cancel_reason": "",
        "rescheduled_from": None,
        "reminded_at": None,
    }
    try:
        await get_db().bookings.insert_one(doc)
    except Exception as exc:
        if "duplicate" in str(exc).lower() or "E11000" in str(exc):
            # Somebody else's insert landed first. This is the index doing its job.
            raise BookingError("Someone just took that slot. Pick another one.") from exc
        raise
    return doc


async def get_booking(booking_id: str, *, user_id: str | None = None) -> dict[str, Any] | None:
    query: dict[str, Any] = {"_id": booking_id}
    if user_id:
        query["user_id"] = user_id
    return await get_db().bookings.find_one(query)


async def list_bookings(
    user_id: str, *, upcoming: bool | None = None, limit: int = 30
) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"user_id": user_id}
    if upcoming is True:
        query["status"] = {"$in": list(HOLDS_SLOT)}
        query["starts_at"] = {"$gte": utcnow() - timedelta(hours=2)}
    elif upcoming is False:
        query["$or"] = [
            {"status": {"$in": [CANCELLED, COMPLETED]}},
            {"starts_at": {"$lt": utcnow() - timedelta(hours=2)}},
        ]
    direction = 1 if upcoming is True else -1
    cursor = get_db().bookings.find(query).sort("starts_at", direction).limit(limit)
    return await cursor.to_list(length=limit)


async def cancel_booking(
    booking: dict[str, Any], *, reason: str = "", by_staff: bool = False
) -> dict[str, Any]:
    if booking.get("status") not in HOLDS_SLOT:
        raise BookingError("That session is not active.")
    window = int(booking.get("cancel_window_hours", DEFAULT_CANCEL_WINDOW))
    if not by_staff and not _can_change(booking, window):
        raise BookingError(
            f"Sessions can be cancelled up to {window} hours beforehand. "
            "Message support and we will sort it out."
        )
    updated = await get_db().bookings.find_one_and_update(
        # The status in the filter is what stops two cancels racing.
        {"_id": booking["_id"], "status": {"$in": list(HOLDS_SLOT)}},
        {
            "$set": {
                "status": CANCELLED,
                "cancelled_at": utcnow(),
                "updated_at": utcnow(),
                "cancel_reason": (reason or "").strip()[:300],
                "cancelled_by": "staff" if by_staff else "user",
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise BookingError("That session is not active.")
    return updated


async def reschedule_booking(
    *, booking: dict[str, Any], coach: dict[str, Any], starts_at: datetime
) -> dict[str, Any]:
    """Book the new slot first, then release the old one.

    In that order a failure leaves the original session intact. Releasing first
    and failing to rebook would lose the slot the person already had.
    """
    window = int(booking.get("cancel_window_hours", DEFAULT_CANCEL_WINDOW))
    if not _can_change(booking, window):
        raise BookingError(
            f"Sessions can be moved up to {window} hours beforehand."
        )
    availability = await available_slots(coach, session_type_id=booking.get("session_type"))
    if not _slot_is_offered(availability, starts_at):
        raise BookingError("That time is no longer free. Pick another slot.")

    start = starts_at.astimezone(timezone.utc).replace(microsecond=0)
    if start == _aware(booking["starts_at"]):
        return booking

    replacement = {
        **{k: v for k, v in booking.items() if k not in ("_id", "created_at")},
        "_id": new_id("bkg"),
        "starts_at": start,
        "ends_at": start + timedelta(minutes=int(booking.get("minutes") or 45)),
        "status": CONFIRMED,
        "created_at": utcnow(),
        "updated_at": utcnow(),
        "rescheduled_from": booking["_id"],
        "reminded_at": None,
    }
    try:
        await get_db().bookings.insert_one(replacement)
    except Exception as exc:
        if "duplicate" in str(exc).lower() or "E11000" in str(exc):
            raise BookingError("Someone just took that slot. Pick another one.") from exc
        raise

    await get_db().bookings.update_one(
        {"_id": booking["_id"]},
        {
            "$set": {
                "status": CANCELLED,
                "cancelled_at": utcnow(),
                "updated_at": utcnow(),
                "cancel_reason": "Moved to another time",
                "cancelled_by": "user",
                "rescheduled_to": replacement["_id"],
            }
        },
    )
    return replacement


async def complete_past_sessions() -> int:
    """Mark finished sessions complete so they leave the upcoming list."""
    result = await get_db().bookings.update_many(
        {"status": CONFIRMED, "ends_at": {"$lt": utcnow() - timedelta(hours=2)}},
        {"$set": {"status": COMPLETED, "updated_at": utcnow()}},
    )
    return int(result.modified_count)


async def due_reminders(within_hours: int = 24) -> list[dict[str, Any]]:
    """Confirmed sessions starting soon that have not been reminded about."""
    cursor = get_db().bookings.find(
        {
            "status": CONFIRMED,
            "reminded_at": None,
            "starts_at": {"$gte": utcnow(), "$lte": utcnow() + timedelta(hours=within_hours)},
        }
    )
    return await cursor.to_list(length=200)


async def mark_reminded(booking_id: str) -> None:
    await get_db().bookings.update_one({"_id": booking_id}, {"$set": {"reminded_at": utcnow()}})


def format_when(booking: dict[str, Any]) -> str:
    """A human sentence for emails, in the coach's timezone."""
    start = _aware(booking.get("starts_at"))
    if not start:
        return "shortly"
    local = start.astimezone(_zone(booking.get("timezone")))
    return local.strftime("%A %-d %B, %H:%M (%Z)")
