"""Daily video-processing quota: FIFO schedule and expected start times.

The hard cap (how many jobs may *start* on a UTC day) is a counter in
`quota_days`, incremented only by the video worker. This module never touches
`started`. It walks the queued FIFO, writes `available_at` / `scheduled_date` /
`expected_start_at`, and notifies when the promised UTC date moves.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from pymongo import ReturnDocument, UpdateOne

from app.config import get_settings
from app.db.mongo import get_db
from app.pipeline.eta import DEFAULT_TOTAL_SECONDS, _historical_average_seconds, estimate_eta_seconds
from app.services import email_service, notification_service

log = logging.getLogger("criclab.quota")

QUOTA_STATE_ID = "global"
_IN_FLIGHT = ("claimed", "processing", "analyzing")
_QUEUED = "queued"


@dataclass(frozen=True)
class QueueItem:
    id: str
    collection: str
    kind: str
    user_id: str
    created_at: datetime
    scheduled_date: str | None = None
    expected_start_at: datetime | None = None


@dataclass(frozen=True)
class Assignment:
    id: str
    collection: str
    kind: str
    user_id: str
    available_at: datetime
    scheduled_date: str
    expected_start_at: datetime
    previous_scheduled_date: str | None


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def utc_day_id(dt: datetime | None = None) -> str:
    now = _aware(dt) or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d")


def utc_midnight(day: str) -> datetime:
    year, month, day_n = (int(p) for p in day.split("-"))
    return datetime(year, month, day_n, tzinfo=timezone.utc)


def next_utc_midnight(now: datetime | None = None) -> datetime:
    when = _aware(now) or datetime.now(timezone.utc)
    when = when.astimezone(timezone.utc)
    return when.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)


def queued_eligible_filter(now: datetime | None = None) -> dict[str, Any]:
    """Queued jobs whose scheduled day has opened (or pre-quota rows with no date)."""
    when = _aware(now) or datetime.now(timezone.utc)
    return {
        "status": _QUEUED,
        "$or": [
            {"available_at": {"$lte": when}},
            {"available_at": {"$exists": False}},
            {"available_at": None},
        ],
    }


async def has_claimable_job(now: datetime | None = None) -> bool:
    """True when the worker could start a clip immediately (slot left + eligible queued)."""
    settings = get_settings()
    when = _aware(now) or datetime.now(timezone.utc)
    if await started_on(utc_day_id(when)) >= int(settings.daily_video_quota):
        return False
    filt = queued_eligible_filter(when)
    db = get_db()
    if await db.jobs.find_one(filt, {"_id": 1}):
        return True
    if await db.balltrack_jobs.find_one(filt, {"_id": 1}):
        return True
    return False


def format_expected_when(dt: datetime) -> str:
    aware = _aware(dt) or datetime.now(timezone.utc)
    aware = aware.astimezone(timezone.utc)
    return f"{aware.day} {aware.strftime('%B %Y, %H:%M')} UTC"


def processing_link(kind: str, job_id: str) -> str:
    if kind == "ballflight":
        return f"/app/ball-flight/processing/{job_id}"
    return f"/app/processing/{job_id}"


def assign_schedule(
    *,
    now: datetime,
    quota: int,
    started_today: int,
    queued: Sequence[QueueItem],
    in_flight_remaining_seconds: float = 0.0,
    duration_seconds: dict[str, float] | None = None,
) -> list[Assignment]:
    """Assign each queued job a UTC day and expected start, FIFO by created_at.

    Pure: no I/O. `queued` must already be oldest-first.
    """
    if quota < 1:
        raise ValueError("quota must be at least 1")
    now = _aware(now) or datetime.now(timezone.utc)
    now = now.astimezone(timezone.utc)
    durations = duration_seconds or dict(DEFAULT_TOTAL_SECONDS)

    day_start = utc_midnight(utc_day_id(now))
    day_end = day_start + timedelta(days=1)
    slots_left = max(0, int(quota) - max(0, int(started_today)))
    cursor = now + timedelta(seconds=max(0.0, float(in_flight_remaining_seconds)))

    def _roll_forward() -> None:
        nonlocal day_start, day_end, slots_left, cursor
        day_start = day_end
        day_end = day_start + timedelta(days=1)
        cursor = max(cursor, day_start)
        slots_left = int(quota)

    while slots_left <= 0 or cursor >= day_end:
        _roll_forward()

    out: list[Assignment] = []
    for item in queued:
        while slots_left <= 0 or cursor >= day_end:
            _roll_forward()
        created = _aware(item.created_at) or now
        expected = cursor
        scheduled = utc_day_id(day_start)
        available = max(created, day_start)
        out.append(
            Assignment(
                id=item.id,
                collection=item.collection,
                kind=item.kind,
                user_id=item.user_id,
                available_at=available,
                scheduled_date=scheduled,
                expected_start_at=expected,
                previous_scheduled_date=item.scheduled_date,
            )
        )
        slots_left -= 1
        kind_dur = float(durations.get(item.kind) or DEFAULT_TOTAL_SECONDS.get(item.kind, 150.0))
        cursor = cursor + timedelta(seconds=max(1.0, kind_dur))
    return out


def _item_from_doc(doc: dict[str, Any], *, collection: str, kind: str) -> QueueItem:
    return QueueItem(
        id=str(doc["_id"]),
        collection=collection,
        kind=kind,
        user_id=str(doc.get("user_id") or ""),
        created_at=_aware(doc.get("created_at")) or datetime.now(timezone.utc),
        scheduled_date=doc.get("scheduled_date"),
        expected_start_at=_aware(doc.get("expected_start_at")),
    )


async def started_on(day: str) -> int:
    doc = await get_db().quota_days.find_one({"_id": day})
    return int((doc or {}).get("started") or 0)


async def _load_queued() -> list[QueueItem]:
    db = get_db()
    action = await db.jobs.find({"status": _QUEUED}).to_list(length=20_000)
    flight = await db.balltrack_jobs.find({"status": _QUEUED}).to_list(length=20_000)
    items = [_item_from_doc(d, collection="jobs", kind="action") for d in action]
    items.extend(_item_from_doc(d, collection="balltrack_jobs", kind="ballflight") for d in flight)
    items.sort(key=lambda i: (i.created_at, i.id))
    return items


async def _in_flight_remaining_seconds() -> float:
    """Seconds until the current clip(s) should free a worker. One process → max remaining."""
    db = get_db()
    remainings: list[float] = []
    action = await db.jobs.find({"status": {"$in": list(_IN_FLIGHT)}}).to_list(length=20)
    flight = await db.balltrack_jobs.find({"status": {"$in": list(_IN_FLIGHT)}}).to_list(length=20)
    for doc in action:
        eta = await estimate_eta_seconds(collection="jobs", pipeline="action", job=doc)
        remainings.append(float(eta if eta is not None else DEFAULT_TOTAL_SECONDS["action"]))
    for doc in flight:
        eta = await estimate_eta_seconds(collection="balltrack_jobs", pipeline="ballflight", job=doc)
        remainings.append(float(eta if eta is not None else DEFAULT_TOTAL_SECONDS["ballflight"]))
    return max(remainings) if remainings else 0.0


async def _duration_seconds() -> dict[str, float]:
    action = await _historical_average_seconds("jobs", "action")
    flight = await _historical_average_seconds("balltrack_jobs", "ballflight")
    return {
        "action": float(action or DEFAULT_TOTAL_SECONDS["action"]),
        "ballflight": float(flight or DEFAULT_TOTAL_SECONDS["ballflight"]),
    }


def _queued_message(kind: str, scheduled_date: str, expected: datetime, today: str) -> str:
    if scheduled_date == today:
        return "Queued for ball tracking" if kind == "ballflight" else "Queued for analysis"
    return f"We'll start this clip on {format_expected_when(expected)}."


async def _notify_insert(assignment: Assignment, today: str) -> None:
    when = format_expected_when(assignment.expected_start_at)
    link = processing_link(assignment.kind, assignment.id)
    await notification_service.notify(
        user_id=assignment.user_id,
        kind="analysis",
        title="Clip received",
        body=f"We'll start this clip around {when}.",
        link=link,
        meta={"job_id": assignment.id, "scheduled_date": assignment.scheduled_date},
    )
    if assignment.scheduled_date > today:
        _schedule_email(
            assignment.user_id,
            "When we'll start your clip",
            f"Thanks for sending your clip. We'll start it around {when}.",
        )


async def _notify_date_change(assignment: Assignment) -> None:
    when = format_expected_when(assignment.expected_start_at)
    prev = assignment.previous_scheduled_date or ""
    sooner = bool(prev) and assignment.scheduled_date < prev
    title = "Your clip will start sooner" if sooner else "Your clip start time moved"
    body = f"We'll now start this clip around {when}."
    await notification_service.notify(
        user_id=assignment.user_id,
        kind="analysis",
        title=title,
        body=body,
        link=processing_link(assignment.kind, assignment.id),
        meta={
            "job_id": assignment.id,
            "scheduled_date": assignment.scheduled_date,
            "previous_scheduled_date": prev,
        },
    )
    _schedule_email(assignment.user_id, title, body)


def _schedule_email(user_id: str, title: str, body: str) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_send_analysis_email(user_id, title, body))


async def _send_analysis_email(user_id: str, title: str, body: str) -> None:
    try:
        from app.db import auth_repo

        user = await auth_repo.get_user(user_id)
        email = (user or {}).get("email")
        if not email:
            return
        name = (user or {}).get("name") or "there"
        await email_service.send_system_notice(email, name, title, body)
    except Exception:
        log.warning("analysis email not sent for %s", user_id, exc_info=False)


async def schedule_queued_jobs(*, notify_inserted_id: str | None = None) -> list[Assignment]:
    """Rewrite schedule fields on every queued job. Never raises to the caller."""
    try:
        settings = get_settings()
        now = datetime.now(timezone.utc)
        today = utc_day_id(now)
        queued = await _load_queued()
        if not queued and notify_inserted_id is None:
            return []
        assignments = assign_schedule(
            now=now,
            quota=int(settings.daily_video_quota),
            started_today=await started_on(today),
            queued=queued,
            in_flight_remaining_seconds=await _in_flight_remaining_seconds(),
            duration_seconds=await _duration_seconds(),
        )
        db = get_db()
        ops: dict[str, list[UpdateOne]] = {"jobs": [], "balltrack_jobs": []}
        for a in assignments:
            ops[a.collection].append(
                UpdateOne(
                    {"_id": a.id, "status": _QUEUED},
                    {
                        "$set": {
                            "available_at": a.available_at,
                            "scheduled_date": a.scheduled_date,
                            "expected_start_at": a.expected_start_at,
                            "message": _queued_message(a.kind, a.scheduled_date, a.expected_start_at, today),
                            "updated_at": now,
                        }
                    },
                )
            )
        for collection, batch in ops.items():
            if batch:
                await db[collection].bulk_write(batch, ordered=False)
        for a in assignments:
            if a.id == notify_inserted_id:
                await _notify_insert(a, today)
            elif a.previous_scheduled_date and a.previous_scheduled_date != a.scheduled_date:
                await _notify_date_change(a)
        return assignments
    except Exception:
        log.exception("quota schedule failed")
        return []
    finally:
        from app.services.ec2_worker import maybe_wake_worker

        maybe_wake_worker()


async def mark_dirty() -> None:
    await get_db().quota_state.update_one(
        {"_id": QUOTA_STATE_ID},
        {"$set": {"dirty_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


async def drain_dirty() -> None:
    db = get_db()
    state = await db.quota_state.find_one({"_id": QUOTA_STATE_ID})
    dirty = (state or {}).get("dirty_at")
    if not dirty:
        return
    await schedule_queued_jobs()
    await db.quota_state.update_one(
        {"_id": QUOTA_STATE_ID, "dirty_at": dirty},
        {"$unset": {"dirty_at": ""}},
    )


async def recompute_loop() -> None:
    """Lifespan task: pick up worker fail/complete/stale-requeue via quota_state.dirty_at."""
    while True:
        await asyncio.sleep(5)
        try:
            await drain_dirty()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("quota dirty drain failed", exc_info=True)


async def midnight_wake_loop(*, slice_seconds: float = 30.0) -> None:
    """Boot catch-up, then recompute + maybe-wake at each 00:00 UTC.

    The always-on API starts the worker EC2; this is not a cron/EventBridge job.
    """

    async def _tick() -> None:
        try:
            await schedule_queued_jobs()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("quota schedule before worker wake failed", exc_info=True)

    await _tick()
    target = next_utc_midnight()
    chunk = max(0.1, float(slice_seconds))
    while True:
        remaining = (target - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            await _tick()
            target = next_utc_midnight()
            continue
        await asyncio.sleep(min(chunk, remaining))


_STALE_IN_FLIGHT = timedelta(minutes=45)
_ACTIVE_CANCEL = (_QUEUED, *_IN_FLIGHT)


async def fail_stale_in_flight_jobs() -> int:
    """Mark claimed/processing jobs with no progress for 45+ minutes as failed.

    A dead worker leaves rows in `processing` forever. The header polls this
    collection, so ghosts stay on screen until they are closed out.
    """
    cutoff = datetime.now(timezone.utc) - _STALE_IN_FLIGHT
    now = datetime.now(timezone.utc)
    fields = {
        "status": "failed",
        "stage": "failed",
        "message": "Analysis stopped before this clip finished. Upload it again.",
        "updated_at": now,
    }
    filt = {
        "status": {"$in": list(_IN_FLIGHT)},
        "$or": [
            {"updated_at": {"$lt": cutoff}},
            {"updated_at": {"$exists": False}, "created_at": {"$lt": cutoff}},
        ],
    }
    n = 0
    db = get_db()
    for name in ("jobs", "balltrack_jobs"):
        n += (await db[name].update_many(filt, {"$set": fields})).modified_count
    return n


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def fail_stale_job(job: dict[str, Any] | None, *, collection: str) -> dict[str, Any] | None:
    """Close one abandoned in-flight job so GET /jobs/:id stops looking live."""
    if not job or job.get("status") not in _IN_FLIGHT:
        return job
    stamp = _aware(job.get("updated_at")) or _aware(job.get("created_at"))
    if stamp is None or datetime.now(timezone.utc) - stamp < _STALE_IN_FLIGHT:
        return job
    now = datetime.now(timezone.utc)
    col = get_db()[collection]
    await col.update_one(
        {"_id": job["_id"], "status": {"$in": list(_IN_FLIGHT)}},
        {
            "$set": {
                "status": "failed",
                "stage": "failed",
                "message": "Analysis stopped before this clip finished. Upload it again.",
                "updated_at": now,
            }
        },
    )
    return await col.find_one({"_id": job["_id"]}) or job


async def cancel_queued_job(*, job_id: str, collection: str) -> dict[str, Any] | None:
    """Cancel a queued or abandoned in-flight job. None if it already finished."""
    db = get_db()
    col = db[collection]
    now = datetime.now(timezone.utc)
    result = await col.find_one_and_update(
        {"_id": job_id, "status": {"$in": list(_ACTIVE_CANCEL)}},
        {
            "$set": {
                "status": "cancelled",
                "stage": "cancelled",
                "message": "Removed from the queue",
                "cancelled_at": now,
                "updated_at": now,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not result:
        return None
    await schedule_queued_jobs()
    return result
