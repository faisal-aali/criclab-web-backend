"""Platform-wide metrics and cross-account queries for the admin panel.

Everything in this module reads across every user's data. Nowhere else in the
codebase does that — every other service scopes its own queries to one
account. That is precisely why every entry point into this module sits behind
`AdminUser` in `app/api/admin.py`, and why this module contains no endpoint
definitions of its own: a query that ignores ownership must never be one
`Depends()` away from being reachable by anyone else.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from app.db.mongo import get_db
from app.db.repository import new_id, utcnow
from app.services import booking_service as bookings
from app.services import support_service as tickets

RangeKey = Literal["today", "7d", "30d", "90d"]
_RANGE_DAYS: dict[str, int] = {"today": 1, "7d": 7, "30d": 30, "90d": 90}


def _aware(dt: Any) -> datetime | None:
    if not dt:
        return None
    return dt if getattr(dt, "tzinfo", None) else dt.replace(tzinfo=timezone.utc)


def resolve_range(range_key: str | None, date_from: str | None, date_to: str | None) -> tuple[datetime, datetime]:
    """A custom `from`/`to` wins when both are given; otherwise a named range."""
    now = utcnow()
    if date_from and date_to:
        try:
            start = datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc)
            end = datetime.fromisoformat(date_to).replace(tzinfo=timezone.utc) + timedelta(days=1)
            if end > start:
                return start, end
        except ValueError:
            pass
    days = _RANGE_DAYS.get(range_key or "30d", 30)
    start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, now


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #

async def dashboard_summary(start: datetime, end: datetime) -> dict[str, Any]:
    db = get_db()
    now = utcnow()
    day_ago = now - timedelta(days=1)
    week_start = now - timedelta(days=7)
    month_start = now - timedelta(days=30)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    users_total = await db.users.count_documents({})
    users_new = await db.users.count_documents({"created_at": {"$gte": start, "$lt": end}})
    # "Active" = signed in within the last day; a user who has never logged in
    # since this field was introduced simply has no `last_login_at` yet.
    users_active = await db.users.count_documents({"last_login_at": {"$gte": day_ago}})
    users_disabled = await db.users.count_documents({"disabled": True})
    users_unverified = await db.users.count_documents({"email_verified": {"$ne": True}})
    users_inactive = max(0, users_total - users_active - users_disabled)

    processing_statuses = ["queued", "processing", "analyzing"]

    def video_counts(collection: str) -> dict[str, Any]:
        col = db[collection]
        return {
            "range": col.count_documents({"status": "completed", "created_at": {"$gte": start, "$lt": end}}),
            "today": col.count_documents({"status": "completed", "created_at": {"$gte": today_start}}),
            "week": col.count_documents({"status": "completed", "created_at": {"$gte": week_start}}),
            "month": col.count_documents({"status": "completed", "created_at": {"$gte": month_start}}),
            "total": col.count_documents({"status": "completed"}),
            "failed_range": col.count_documents({"status": "failed", "created_at": {"$gte": start, "$lt": end}}),
            "processing": col.count_documents({"status": {"$in": processing_statuses}}),
            "failed_total": col.count_documents({"status": "failed"}),
        }

    action = video_counts("jobs")
    ball = video_counts("balltrack_jobs")
    for key in list(action):
        action[key] = await action[key]
    for key in list(ball):
        ball[key] = await ball[key]

    coaching_total = await db.bookings.count_documents({})
    coaching_upcoming = await db.bookings.count_documents(
        {"status": {"$in": list(bookings.HOLDS_SLOT)}, "starts_at": {"$gte": now}}
    )
    coaching_completed = await db.bookings.count_documents({"status": bookings.COMPLETED})
    coaching_cancelled = await db.bookings.count_documents({"status": bookings.CANCELLED})

    tickets_open = await db.tickets.count_documents({"status": {"$in": list(tickets.LIVE_STATUSES)}})
    tickets_resolved = await db.tickets.count_documents({"status": {"$in": [tickets.RESOLVED, tickets.CLOSED]}})

    recent_throws = await _recent_throws(limit=12)
    in_progress = await _in_progress(limit=8)

    return {
        "range": {"from": start, "to": end},
        "users": {
            "total": users_total,
            "new": users_new,
            "active": users_active,
            "inactive": users_inactive,
            "disabled": users_disabled,
            "unverified": users_unverified,
        },
        "videos": {
            "total": action["total"] + ball["total"],
            "range": action["range"] + ball["range"],
            "today": action["today"] + ball["today"],
            "week": action["week"] + ball["week"],
            "month": action["month"] + ball["month"],
            "failed_range": action["failed_range"] + ball["failed_range"],
            "processing": action["processing"] + ball["processing"],
            "failed_total": action["failed_total"] + ball["failed_total"],
            "by_pipeline": {"action": action["total"], "ball_flight": ball["total"]},
        },
        "coaching": {
            "total": coaching_total,
            "upcoming": coaching_upcoming,
            "completed": coaching_completed,
            "cancelled": coaching_cancelled,
        },
        "support": {
            "open": tickets_open,
            "resolved": tickets_resolved,
        },
        "recent_throws": recent_throws,
        "in_progress": in_progress,
    }


async def signups_trend(start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Daily signup counts across the range — the dashboard's line chart."""
    pipeline = [
        {"$match": {"created_at": {"$gte": start, "$lt": end}}},
        {"$group": {"_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}}, "n": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]
    rows = await get_db().users.aggregate(pipeline).to_list(length=400)
    return [{"date": r["_id"], "count": r["n"]} for r in rows]


async def analyses_trend(start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Daily completed-analysis counts, both pipelines combined."""
    counts: dict[str, int] = {}
    for collection in ("jobs", "balltrack_jobs"):
        pipeline = [
            {"$match": {"status": "completed", "created_at": {"$gte": start, "$lt": end}}},
            {"$group": {"_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}}, "n": {"$sum": 1}}},
        ]
        rows = await get_db()[collection].aggregate(pipeline).to_list(length=400)
        for r in rows:
            counts[r["_id"]] = counts.get(r["_id"], 0) + r["n"]
    return [{"date": d, "count": n} for d, n in sorted(counts.items())]


# --------------------------------------------------------------------------- #
# What a player threw — numbers copied off the stored metrics JSON
# --------------------------------------------------------------------------- #

def _metric_num(metrics: dict[str, Any] | None, key: str) -> float | None:
    if not metrics:
        return None
    node = metrics.get(key)
    if not isinstance(node, dict):
        return None
    if node.get("status") not in (None, "ok", "estimated"):
        return None
    val = node.get("value")
    return float(val) if isinstance(val, (int, float)) else None


def _throw_fields(metrics: dict[str, Any] | None) -> dict[str, Any]:
    metrics = metrics or {}
    profile = metrics.get("player_profile") or {}
    dtype = metrics.get("delivery_type") or {}
    scores = metrics.get("scores") or {}
    pace = dtype.get("value") if isinstance(dtype, dict) and dtype.get("status") == "ok" else None
    return {
        "ball_speed_kmh": _metric_num(metrics, "ball_speed_kmh"),
        "arm_speed_kmh": _metric_num(metrics, "arm_speed_kmh"),
        "delivery_type": pace,
        "bowling_arm": profile.get("bowling_arm"),
        "bowling_style": profile.get("bowling_style"),
        "overall_score": scores.get("overall") if isinstance(scores.get("overall"), (int, float)) else None,
    }


async def _owners_for(user_ids: list[str]) -> dict[str, dict[str, str | None]]:
    owners: dict[str, dict[str, str | None]] = {}
    ids = [i for i in user_ids if i]
    if not ids:
        return owners
    async for u in get_db().users.find({"_id": {"$in": ids}}, {"name": 1, "email": 1}):
        owners[u["_id"]] = {"name": u.get("name"), "email": u.get("email")}
    return owners


async def _recent_throws(limit: int = 12) -> list[dict[str, Any]]:
    """Latest completed Action deliveries — the dashboard's 'what they threw' list."""
    db = get_db()
    rows = await db.deliveries.find({}).sort("created_at", -1).limit(limit).to_list(limit)
    owners = await _owners_for([r.get("user_id") for r in rows])
    out = []
    for d in rows:
        item = {
            "id": d["_id"],
            "pipeline": "action",
            "result_id": d["_id"],
            "user_id": d.get("user_id"),
            "user": owners.get(d.get("user_id")),
            "player_name": d.get("player_name"),
            "created_at": d.get("created_at"),
            "summary": (d.get("analysis") or {}).get("summary"),
            **_throw_fields(d.get("metrics")),
        }
        out.append(item)
    return out


async def _in_progress(limit: int = 8) -> list[dict[str, Any]]:
    db = get_db()
    live = {"status": {"$in": ["queued", "processing", "analyzing"]}}
    jobs = await db.jobs.find(live).sort("updated_at", -1).limit(limit).to_list(limit)
    ball = await db.balltrack_jobs.find(live).sort("updated_at", -1).limit(limit).to_list(limit)
    rows = []
    for j in jobs:
        rows.append(
            {
                "id": j["_id"],
                "pipeline": "action",
                "player_name": j.get("player_name"),
                "user_id": j.get("user_id"),
                "status": j.get("status"),
                "stage": j.get("stage"),
                "progress": j.get("progress"),
                "updated_at": j.get("updated_at"),
            }
        )
    for j in ball:
        rows.append(
            {
                "id": j["_id"],
                "pipeline": "ball_flight",
                "player_name": None,
                "user_id": j.get("user_id"),
                "status": j.get("status"),
                "stage": j.get("stage"),
                "progress": j.get("progress"),
                "updated_at": j.get("updated_at"),
            }
        )
    rows.sort(key=lambda r: r.get("updated_at") or now_floor(), reverse=True)
    page = rows[:limit]
    owners = await _owners_for([r.get("user_id") for r in page])
    for r in page:
        r["user"] = owners.get(r.get("user_id"))
    return page


async def _attach_results(rows: list[dict[str, Any]]) -> None:
    """Fill `result_id` + throw numbers for a page of job-shaped analysis rows."""
    db = get_db()
    action_ids = [r["id"] for r in rows if r.get("pipeline") == "action"]
    by_job: dict[str, dict[str, Any]] = {}
    if action_ids:
        async for d in db.deliveries.find(
            {"job_id": {"$in": action_ids}},
            {"job_id": 1, "metrics": 1, "analysis": 1},
        ):
            by_job[d["job_id"]] = d
    ball_ids = [r["id"] for r in rows if r.get("pipeline") == "ball_flight"]
    session_by_job: dict[str, str | None] = {}
    if ball_ids:
        async for j in db.balltrack_jobs.find({"_id": {"$in": ball_ids}}, {"session_id": 1}):
            session_by_job[j["_id"]] = j.get("session_id")
    for r in rows:
        if r.get("pipeline") == "ball_flight":
            r["result_id"] = session_by_job.get(r["id"])
            continue
        d = by_job.get(r["id"])
        if not d:
            r["result_id"] = None
            continue
        r["result_id"] = d["_id"]
        r.update(_throw_fields(d.get("metrics")))
        r["summary"] = (d.get("analysis") or {}).get("summary")



# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #

def public_admin_user(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": doc["_id"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "role": doc.get("role", "user"),
        "email_verified": bool(doc.get("email_verified")),
        "disabled": bool(doc.get("disabled")),
        "created_at": doc.get("created_at"),
        "last_login_at": doc.get("last_login_at"),
    }


async def list_users(
    *, search: str | None, status: str | None, page: int, page_size: int
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if search:
        needle = search.strip()
        query["$or"] = [
            {"email": {"$regex": needle, "$options": "i"}},
            {"name": {"$regex": needle, "$options": "i"}},
        ]
    if status == "disabled":
        query["disabled"] = True
    elif status == "active":
        query["disabled"] = {"$ne": True}
    elif status == "unverified":
        query["email_verified"] = {"$ne": True}

    db = get_db()
    total = await db.users.count_documents(query)
    cursor = (
        db.users.find(query)
        .sort("created_at", -1)
        .skip(max(0, (page - 1) * page_size))
        .limit(page_size)
    )
    rows = await cursor.to_list(length=page_size)

    # One small aggregate per activity type across just this page's ids —
    # cheap at admin list-page scale (tens of rows), and far simpler than a
    # $lookup across four differently-shaped collections.
    ids = [r["_id"] for r in rows]
    analyses = await _counts_by_user(["jobs", "balltrack_jobs"], ids)
    booking_counts = await _counts_by_user(["bookings"], ids)
    ticket_counts = await _counts_by_user(["tickets"], ids)

    items = []
    for r in rows:
        uid = r["_id"]
        item = public_admin_user(r)
        item["analysis_count"] = analyses.get(uid, 0)
        item["booking_count"] = booking_counts.get(uid, 0)
        item["ticket_count"] = ticket_counts.get(uid, 0)
        items.append(item)

    return {"items": items, "total": total, "page": page, "page_size": page_size}


async def _counts_by_user(collections: list[str], user_ids: list[str]) -> dict[str, int]:
    if not user_ids:
        return {}
    totals: dict[str, int] = {}
    for collection in collections:
        pipeline = [
            {"$match": {"user_id": {"$in": user_ids}}},
            {"$group": {"_id": "$user_id", "n": {"$sum": 1}}},
        ]
        rows = await get_db()[collection].aggregate(pipeline).to_list(length=len(user_ids))
        for row in rows:
            totals[row["_id"]] = totals.get(row["_id"], 0) + row["n"]
    return totals


async def user_detail(user_id: str) -> dict[str, Any] | None:
    doc = await get_db().users.find_one({"_id": user_id})
    if not doc:
        return None
    out = public_admin_user(doc)

    action_jobs = await get_db().jobs.find({"user_id": user_id}).sort("created_at", -1).limit(10).to_list(10)
    ball_jobs = (
        await get_db().balltrack_jobs.find({"user_id": user_id}).sort("created_at", -1).limit(10).to_list(10)
    )
    recent_analyses = sorted(
        [
            {
                "id": j["_id"],
                "pipeline": "action",
                "status": j.get("status"),
                "player_name": j.get("player_name"),
                "created_at": j.get("created_at"),
            }
            for j in action_jobs
        ]
        + [
            {
                "id": j["_id"],
                "pipeline": "ball_flight",
                "status": j.get("status"),
                "player_name": None,
                "created_at": j.get("created_at"),
            }
            for j in ball_jobs
        ],
        key=lambda x: x["created_at"] or utcnow(),
        reverse=True,
    )[:10]

    recent_bookings = await get_db().bookings.find({"user_id": user_id}).sort("starts_at", -1).limit(10).to_list(10)
    recent_tickets = await get_db().tickets.find({"user_id": user_id}).sort("updated_at", -1).limit(10).to_list(10)

    out["recent_analyses"] = recent_analyses
    out["recent_bookings"] = [
        {
            "id": b["_id"],
            "coach_name": b.get("coach_name"),
            "status": b.get("status"),
            "starts_at": b.get("starts_at"),
        }
        for b in recent_bookings
    ]
    out["recent_tickets"] = [
        {"id": t["_id"], "subject": t.get("subject"), "status": t.get("status"), "updated_at": t.get("updated_at")}
        for t in recent_tickets
    ]
    await _attach_results(out["recent_analyses"])
    return out


async def user_history(user_id: str, limit: int = 50) -> dict[str, Any] | None:
    """Every Action delivery and ball-flight session this account owns.

    Same numbers the player sees on History / Results — staff is looking at
    the stored metrics JSON, not a second copy of the measurement.
    """
    doc = await get_db().users.find_one({"_id": user_id}, {"name": 1, "email": 1})
    if not doc:
        return None
    db = get_db()
    deliveries = await db.deliveries.find({"user_id": user_id}).sort("created_at", -1).limit(limit).to_list(limit)
    sessions = (
        await db.balltrack_sessions.find({"user_id": user_id}).sort("created_at", -1).limit(limit).to_list(limit)
    )
    action = [
        {
            "id": d["_id"],
            "pipeline": "action",
            "result_id": d["_id"],
            "player_name": d.get("player_name"),
            "created_at": d.get("created_at"),
            "summary": (d.get("analysis") or {}).get("summary"),
            "delivery_count": 1,
            **_throw_fields(d.get("metrics")),
        }
        for d in deliveries
    ]
    ball = [
        {
            "id": s["_id"],
            "pipeline": "ball_flight",
            "result_id": s["_id"],
            "player_name": s.get("title") or "Ball flight",
            "created_at": s.get("created_at"),
            "summary": None,
            "delivery_count": s.get("delivery_count") or len(s.get("delivery_ids") or []),
            "ball_speed_kmh": None,
            "arm_speed_kmh": None,
            "delivery_type": None,
            "bowling_arm": None,
            "bowling_style": None,
            "overall_score": None,
        }
        for s in sessions
    ]
    items = sorted(action + ball, key=lambda x: x["created_at"] or now_floor(), reverse=True)
    return {
        "user": {"id": doc["_id"], "name": doc.get("name"), "email": doc.get("email")},
        "items": items,
    }


async def set_user_disabled(user_id: str, disabled: bool) -> dict[str, Any] | None:
    return await get_db().users.find_one_and_update(
        {"_id": user_id},
        {"$set": {"disabled": disabled, "updated_at": utcnow()}},
        return_document=True,
    )


# --------------------------------------------------------------------------- #
# Analyses (both pipelines, combined)
# --------------------------------------------------------------------------- #

async def list_analyses(
    *, pipeline: str | None, status: str | None, search: str | None, page: int, page_size: int
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if status:
        query["status"] = status
    if search:
        query["player_name"] = {"$regex": search.strip(), "$options": "i"}

    collections = ["jobs", "balltrack_jobs"] if not pipeline else (
        ["jobs"] if pipeline == "action" else ["balltrack_jobs"]
    )

    rows: list[dict[str, Any]] = []
    for collection in collections:
        tag = "action" if collection == "jobs" else "ball_flight"
        cursor = get_db()[collection].find(query).sort("created_at", -1).limit(page * page_size + page_size)
        async for doc in cursor:
            rows.append(
                {
                    "id": doc["_id"],
                    "pipeline": tag,
                    "user_id": doc.get("user_id"),
                    "player_name": doc.get("player_name"),
                    "status": doc.get("status"),
                    "stage": doc.get("stage"),
                    "progress": doc.get("progress"),
                    "message": doc.get("message"),
                    "created_at": doc.get("created_at"),
                    "updated_at": doc.get("updated_at"),
                }
            )

    rows.sort(key=lambda r: r["created_at"] or now_floor(), reverse=True)
    total = len(rows)
    start = max(0, (page - 1) * page_size)
    page_rows = rows[start : start + page_size]

    user_ids = list({r["user_id"] for r in page_rows if r.get("user_id")})
    owners = {}
    if user_ids:
        async for u in get_db().users.find({"_id": {"$in": user_ids}}, {"name": 1, "email": 1}):
            owners[u["_id"]] = {"name": u.get("name"), "email": u.get("email")}
    for r in page_rows:
        r["user"] = owners.get(r.get("user_id"))
    await _attach_results(page_rows)

    return {"items": page_rows, "total": total, "page": page, "page_size": page_size}


def now_floor() -> datetime:
    return datetime.min.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Coaching (admin-wide bookings view)
# --------------------------------------------------------------------------- #

async def list_all_bookings(*, status: str | None, page: int, page_size: int) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if status == "upcoming":
        query["status"] = {"$in": list(bookings.HOLDS_SLOT)}
        query["starts_at"] = {"$gte": utcnow()}
    elif status in (bookings.COMPLETED, bookings.CANCELLED, bookings.PENDING, bookings.CONFIRMED):
        query["status"] = status

    db = get_db()
    total = await db.bookings.count_documents(query)
    cursor = (
        db.bookings.find(query)
        .sort("starts_at", -1)
        .skip(max(0, (page - 1) * page_size))
        .limit(page_size)
    )
    rows = await cursor.to_list(length=page_size)
    items = [
        {
            "id": b["_id"],
            "user_name": b.get("user_name"),
            "user_email": b.get("user_email"),
            "coach_name": b.get("coach_name"),
            "session_label": b.get("session_label"),
            "starts_at": b.get("starts_at"),
            "status": b.get("status"),
            "created_at": b.get("created_at"),
        }
        for b in rows
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Support metrics
# --------------------------------------------------------------------------- #

async def ticket_metrics() -> dict[str, Any]:
    db = get_db()
    counts = {s: await db.tickets.count_documents({"status": s}) for s in tickets.STATUSES}

    # Average time from creation to resolution, over recently resolved tickets.
    resolved = (
        await db.tickets.find(
            {"status": {"$in": [tickets.RESOLVED, tickets.CLOSED]}, "resolved_at": {"$ne": None}},
            {"created_at": 1, "resolved_at": 1},
        )
        .sort("resolved_at", -1)
        .limit(100)
        .to_list(100)
    )
    durations = []
    for t in resolved:
        created, resolved_at = _aware(t.get("created_at")), _aware(t.get("resolved_at"))
        if created and resolved_at and resolved_at > created:
            durations.append((resolved_at - created).total_seconds())
    avg_resolution_hours = round(sum(durations) / len(durations) / 3600, 1) if durations else None

    return {"counts": counts, "avg_resolution_hours": avg_resolution_hours}


# --------------------------------------------------------------------------- #
# Notifications broadcast
# --------------------------------------------------------------------------- #

async def broadcast_notification(
    *, title: str, body: str, user_ids: list[str] | None, sent_by: str
) -> dict[str, Any]:
    from app.services import notification_service

    if user_ids:
        targets = user_ids
    else:
        targets = [u["_id"] async for u in get_db().users.find({}, {"_id": 1})]

    sent = await notification_service.notify_many(
        targets, kind="system", title=title[:140], body=body[:400], link=None
    )

    record = {
        "_id": new_id("bcast"),
        "title": title[:140],
        "body": body[:400],
        "audience": "selected" if user_ids else "all",
        "recipient_count": sent,
        "sent_by": sent_by,
        "created_at": utcnow(),
    }
    await get_db().admin_broadcasts.insert_one(record)
    return {"sent": sent, "id": record["_id"]}


async def list_broadcasts(limit: int = 30) -> list[dict[str, Any]]:
    cursor = get_db().admin_broadcasts.find({}).sort("created_at", -1).limit(limit)
    rows = await cursor.to_list(length=limit)
    return [
        {
            "id": r["_id"],
            "title": r.get("title"),
            "body": r.get("body"),
            "audience": r.get("audience"),
            "recipient_count": r.get("recipient_count"),
            "created_at": r.get("created_at"),
        }
        for r in rows
    ]
