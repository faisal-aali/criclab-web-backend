from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.api.deps import CurrentUser, VerifiedUser, visible_to
from app.api.videos import _incoming_source_key, _reject_archived_original
from app.balltrack import repo
from app.balltrack.stumps import detect_stump_sets
from app.config import get_settings
from app.pipeline import quota
from app.pipeline.eta import estimate_eta_seconds
from app.services import original_archive, s3_service

router = APIRouter(prefix="/balltrack", tags=["balltrack"])

_VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".webm"}


@router.post("/detect-stumps")
async def detect_stumps(
    file: UploadFile = File(...),
    hints: str | None = Form(None),
):
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Empty image")
    import cv2
    import numpy as np

    buf = np.frombuffer(raw, dtype=np.uint8)
    bgr = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if bgr is None:
        raise HTTPException(400, "Could not read that photo")
    hint_obj = None
    if hints:
        try:
            hint_obj = json.loads(hints)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "hints must be JSON") from exc
    try:
        return detect_stump_sets(bgr, hint_obj)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _signed_artifact(art: dict, key_name: str, legacy_name: str, fallback: str | None) -> str | None:
    signed = s3_service.signed_get(art.get(key_name))
    return signed or art.get(legacy_name) or fallback


def _public_session(doc: dict, deliveries: list[dict] | None = None) -> dict:
    art = dict(doc.get("artifacts") or {})
    job_id = art.get("job_id") or doc.get("job_id")
    local_overlay = f"/balltrack/media/{job_id}/overlay.mp4" if job_id else art.get("overlay_url")
    local_map = f"/balltrack/media/{job_id}/pitch_map.png" if job_id else art.get("pitch_map_url")
    overlay = _signed_artifact(art, "overlay_key", "cloudinary_overlay_url", local_overlay)
    pitch = _signed_artifact(art, "pitch_map_key", "cloudinary_pitch_map_url", local_map)
    compressed = s3_service.signed_get(doc.get("compressed_key") or art.get("compressed_key"))
    out = {
        "id": doc["_id"],
        "title": doc.get("title") or "Session",
        "status": doc.get("status"),
        "created_at": doc.get("created_at"),
        "delivery_count": doc.get("delivery_count") or len(doc.get("delivery_ids") or []),
        "artifacts": {
            **art,
            "overlay_url": overlay,
            "pitch_map_url": pitch,
            "cloudinary_overlay_url": overlay,
            "cloudinary_pitch_map_url": pitch,
            "compressed_video_url": compressed,
        },
        "analysis": doc.get("analysis") or {},
        "error": doc.get("error"),
    }
    if deliveries is not None:
        out["deliveries"] = [_public_delivery(d) for d in deliveries]
    return out


def _public_delivery(d: dict) -> dict:
    art = dict(d.get("artifacts") or {})
    clip = _signed_artifact(art, "clip_key", "cloudinary_clip_url", art.get("clip_url"))
    return {
        "id": d["_id"],
        "session_id": d.get("session_id"),
        "index": d.get("index"),
        "created_at": d.get("created_at"),
        "metrics": d.get("metrics"),
        "bounce": d.get("bounce"),
        "artifacts": {
            **art,
            "clip_url": clip,
            "cloudinary_clip_url": clip,
        },
    }


@router.post("/sessions")
async def create_session(
    user: VerifiedUser,
    file: UploadFile | None = File(None),
    source_key: str | None = Form(None),
    source_url: str | None = Form(None),
    original_name: str | None = Form(None),
    calibration: str = Form(...),
    title: str = Form("Ball Track session"),
    pitch_length_m: float | None = Form(None),
):
    try:
        cal = json.loads(calibration)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "calibration must be JSON") from exc
    if pitch_length_m:
        cal["pitch_length_m"] = pitch_length_m
    if "bowler" not in cal or "batter" not in cal:
        raise HTTPException(400, "calibration needs bowler and batter boxes")

    settings = get_settings()
    videos_dir = settings.storage_path / "balltrack" / "videos"
    videos_dir.mkdir(parents=True, exist_ok=True)
    session_id = repo.new_id("bts")
    job_id = repo.new_id("btj")
    dest = videos_dir / session_id
    stored_name = original_name
    incoming_key = _incoming_source_key(source_key, source_url)
    if incoming_key:
        _reject_archived_original(incoming_key)
        suffix = Path(incoming_key).suffix.lower() or ".mp4"
        if suffix not in _VIDEO_SUFFIXES:
            suffix = ".mp4"
        dest = dest.with_suffix(suffix)
        stored_name = stored_name or Path(incoming_key).name or dest.name
    else:
        if not file or not file.filename:
            raise HTTPException(400, "Attach a video or a source_key")
        suffix = Path(file.filename).suffix.lower() or ".mp4"
        if suffix not in _VIDEO_SUFFIXES:
            raise HTTPException(400, "Unsupported video type")
        dest = dest.with_suffix(suffix)
        with dest.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        stored_name = file.filename

    now = repo.utcnow()
    await repo.insert_session(
        {
            "_id": session_id,
            "user_id": user["_id"],
            "title": title.strip() or "Ball Track session",
            "path": str(dest),
            "source_key": incoming_key,
            "source_url": None,
            "compressed_key": None,
            "job_id": job_id,
            "original_name": stored_name,
            "calibration": cal,
            "status": "queued",
            "delivery_ids": [],
            "created_at": now,
            "updated_at": now,
        }
    )
    await repo.insert_job(
        {
            "_id": job_id,
            "session_id": session_id,
            "user_id": user["_id"],
            "status": "queued",
            "progress": 0,
            "stage": "queued",
            "message": "Queued for ball tracking",
            "created_at": now,
            "updated_at": now,
        }
    )
    await quota.schedule_queued_jobs(notify_inserted_id=job_id)
    return {"session_id": session_id, "job_id": job_id, "status": "queued"}


@router.get("/sessions")
async def list_sessions(user: CurrentUser, limit: int = 50):
    items = await repo.list_sessions(limit=limit, user_id=user["_id"])
    return {"items": [_public_session(s) for s in items]}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, user: CurrentUser):
    s = await repo.get_session(session_id)
    if not visible_to(user, (s or {}).get("user_id"), exists=bool(s)):
        raise HTTPException(404, "Session not found")
    deliveries = await repo.list_deliveries_for_session(session_id)
    return _public_session(s, deliveries)


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, user: CurrentUser):
    job = await repo.get_job(job_id)
    if not visible_to(user, (job or {}).get("user_id"), exists=bool(job)):
        raise HTTPException(404, "Job not found")
    job = await quota.fail_stale_job(job, collection="balltrack_jobs")
    job["id"] = job.pop("_id")
    job["eta_seconds"] = await estimate_eta_seconds(
        collection="balltrack_jobs", pipeline="ballflight", job=job
    )
    return job


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, user: CurrentUser):
    job = await repo.get_job(job_id)
    if not visible_to(user, (job or {}).get("user_id"), exists=bool(job)):
        raise HTTPException(404, "Job not found")
    if job.get("status") not in ("queued", "claimed", "processing", "analyzing"):
        raise HTTPException(409, "This clip has already finished")
    prior = job.get("status")
    updated = await quota.cancel_queued_job(job_id=job_id, collection="balltrack_jobs")
    if not updated:
        raise HTTPException(409, "This clip has already finished")
    if prior == "queued":
        await original_archive.maybe_archive_for_job(updated)
    session_id = updated.get("session_id")
    if session_id:
        await repo.update_session(str(session_id), status="cancelled")
    return {"id": updated["_id"], "status": updated.get("status")}


@router.get("/deliveries/{delivery_id}")
async def get_delivery(delivery_id: str, user: CurrentUser):
    d = await repo.get_delivery(delivery_id)
    if not d:
        raise HTTPException(404, "Delivery not found")
    # Deliveries carry `session_id`, not their own `user_id` — the session is
    # the owned resource, so ownership is settled by looking at its parent.
    session = await repo.get_session(d.get("session_id") or "")
    if not visible_to(user, (session or {}).get("user_id"), exists=bool(session)):
        raise HTTPException(404, "Delivery not found")
    return _public_delivery(d)


@router.get("/media/{job_id}/{filename}")
async def get_media(job_id: str, filename: str):
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "Invalid filename")
    path = get_settings().storage_path / "balltrack" / job_id / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(path)
