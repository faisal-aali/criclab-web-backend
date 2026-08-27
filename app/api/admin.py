"""Admin panel endpoints.

Every route in this file depends on `AdminUser`. That is not a convention to
remember — FastAPI resolves it before the handler body runs at all, so there
is no route here reachable without it, and no route where getting the
dependency order right is left to the person adding the next endpoint. The
frontend also hides this section from anyone but an admin, but that is a
convenience, not the control: the control is this dependency, on every path.

Sensitive actions (disabling an account, broadcasting a notification) are
logged through the same `security_events` audit trail every other sensitive
action in the app uses — see `log_security_event`.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.deps import AdminUser, Client
from app.coaching.recommend import (
    load_catalog,
    parse_youtube_id,
    save_catalog,
    slugify_drill_id,
)
from app.db import auth_repo
from app.services import admin_service as svc

router = APIRouter(prefix="/admin", tags=["admin"])


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #

@router.get("/dashboard")
async def dashboard(
    _: AdminUser,
    range: str = Query("30d", pattern="^(today|7d|30d|90d|custom)$"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
):
    start, end = svc.resolve_range(range, date_from, date_to)
    summary = await svc.dashboard_summary(start, end)
    signups = await svc.signups_trend(start, end)
    analyses = await svc.analyses_trend(start, end)
    return {**summary, "signups_trend": signups, "analyses_trend": analyses}


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #

@router.get("/users")
async def list_users(
    _: AdminUser,
    search: str | None = Query(None, max_length=120),
    status_filter: str | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    return await svc.list_users(search=search, status=status_filter, page=page, page_size=page_size)


@router.get("/users/{user_id}")
async def get_user(user_id: str, _: AdminUser):
    detail = await svc.user_detail(user_id)
    if not detail:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return detail


@router.get("/users/{user_id}/history")
async def user_history(user_id: str, _: AdminUser):
    history = await svc.user_history(user_id)
    if not history:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return history


class UserStatusIn(BaseModel):
    disabled: bool


@router.patch("/users/{user_id}/status")
async def set_user_status(user_id: str, payload: UserStatusIn, admin: AdminUser, client: Client):
    if user_id == admin["_id"] and payload.disabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot disable your own account")
    updated = await svc.set_user_disabled(user_id, payload.disabled)
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    await auth_repo.log_security_event(
        user_id=admin["_id"],
        event="admin_user_disabled" if payload.disabled else "admin_user_enabled",
        ip=client.ip,
        user_agent=client.user_agent,
        detail=f"target={user_id}",
    )
    return {"user": svc.public_admin_user(updated)}


# --------------------------------------------------------------------------- #
# Analyses
# --------------------------------------------------------------------------- #

@router.get("/analyses")
async def list_analyses(
    _: AdminUser,
    pipeline: str | None = Query(None, pattern="^(action|ball_flight)$"),
    status_filter: str | None = Query(None, alias="status"),
    search: str | None = Query(None, max_length=120),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    return await svc.list_analyses(
        pipeline=pipeline, status=status_filter, search=search, page=page, page_size=page_size
    )


# --------------------------------------------------------------------------- #
# Drill library
# --------------------------------------------------------------------------- #

class DrillIn(BaseModel):
    title: str = Field(min_length=3, max_length=140)
    youtube_id: str = Field(min_length=8, max_length=240)
    tags: list[str] = Field(default_factory=list, max_length=12)
    id: str | None = Field(default=None, max_length=48)


def _unique_drill_id(wanted: str, taken: set[str]) -> str:
    base = wanted if re.fullmatch(r"[a-z][a-z0-9_]{1,47}", wanted) else slugify_drill_id(wanted)
    candidate = base
    n = 2
    while candidate in taken:
        candidate = f"{base}_{n}"[:48]
        n += 1
    return candidate


@router.get("/drills")
def list_drills(_: AdminUser):
    items = load_catalog()
    return {"items": items, "tags": sorted({t for d in items for t in d.get("tags") or []})}


@router.post("/drills", status_code=status.HTTP_201_CREATED)
async def create_drill(payload: DrillIn, admin: AdminUser, client: Client):
    items = list(load_catalog())
    taken = {d["id"] for d in items}
    drill_id = _unique_drill_id((payload.id or slugify_drill_id(payload.title)).lower(), taken)
    try:
        youtube_id = parse_youtube_id(payload.youtube_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    items.append(
        {
            "id": drill_id,
            "youtube_id": youtube_id,
            "title": payload.title.strip(),
            "tags": payload.tags,
        }
    )
    try:
        saved = save_catalog(items)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await auth_repo.log_security_event(
        user_id=admin["_id"],
        event="admin_drill_created",
        ip=client.ip,
        user_agent=client.user_agent,
        detail=f"drill={drill_id}",
    )
    return {"item": next(d for d in saved if d["id"] == drill_id)}


@router.put("/drills/{drill_id}")
async def update_drill(drill_id: str, payload: DrillIn, admin: AdminUser, client: Client):
    items = list(load_catalog())
    idx = next((i for i, d in enumerate(items) if d["id"] == drill_id), None)
    if idx is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Drill not found")
    try:
        youtube_id = parse_youtube_id(payload.youtube_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    items[idx] = {
        "id": drill_id,
        "youtube_id": youtube_id,
        "title": payload.title.strip(),
        "tags": payload.tags,
    }
    try:
        saved = save_catalog(items)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await auth_repo.log_security_event(
        user_id=admin["_id"],
        event="admin_drill_updated",
        ip=client.ip,
        user_agent=client.user_agent,
        detail=f"drill={drill_id}",
    )
    return {"item": next(d for d in saved if d["id"] == drill_id)}


@router.delete("/drills/{drill_id}")
async def delete_drill(drill_id: str, admin: AdminUser, client: Client):
    items = list(load_catalog())
    kept = [d for d in items if d["id"] != drill_id]
    if len(kept) == len(items):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Drill not found")
    try:
        save_catalog(kept)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await auth_repo.log_security_event(
        user_id=admin["_id"],
        event="admin_drill_deleted",
        ip=client.ip,
        user_agent=client.user_agent,
        detail=f"drill={drill_id}",
    )
    return {"status": "deleted", "id": drill_id}


# --------------------------------------------------------------------------- #
# Coaching
# --------------------------------------------------------------------------- #

@router.get("/bookings")
async def list_bookings(
    _: AdminUser,
    status_filter: str | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    return await svc.list_all_bookings(status=status_filter, page=page, page_size=page_size)


# --------------------------------------------------------------------------- #
# Support
# --------------------------------------------------------------------------- #

@router.get("/tickets/metrics")
async def ticket_metrics(_: AdminUser):
    return await svc.ticket_metrics()


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #

class BroadcastIn(BaseModel):
    title: str = Field(min_length=1, max_length=140)
    body: str = Field(min_length=1, max_length=400)
    user_ids: list[str] | None = Field(default=None, max_length=500)


@router.post("/notifications/broadcast", status_code=status.HTTP_201_CREATED)
async def broadcast(payload: BroadcastIn, admin: AdminUser, client: Client):
    result = await svc.broadcast_notification(
        title=payload.title, body=payload.body, user_ids=payload.user_ids, sent_by=admin["_id"]
    )
    await auth_repo.log_security_event(
        user_id=admin["_id"],
        event="admin_broadcast_sent",
        ip=client.ip,
        user_agent=client.user_agent,
        detail=f"recipients={result['sent']}",
    )
    return result


@router.get("/notifications/history")
async def broadcast_history(_: AdminUser):
    return {"items": await svc.list_broadcasts()}
