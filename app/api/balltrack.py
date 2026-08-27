from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.api.deps import CurrentUser, VerifiedUser, visible_to
from app.balltrack import repo
from app.balltrack.runner import run_balltrack_job
from app.balltrack.stumps import detect_stump_sets
from app.config import get_settings
from app.pipeline.eta import estimate_eta_seconds

router = APIRouter(prefix="/balltrack", tags=["balltrack"])


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


def _public_session(doc: dict, deliveries: list[dict] | None = None) -> dict:
    out = {
        "id": doc["_id"],
        "title": doc.get("title") or "Session",
        "status": doc.get("status"),
        "created_at": doc.get("created_at"),
        "delivery_count": doc.get("delivery_count") or len(doc.get("delivery_ids") or []),
        "artifacts": doc.get("artifacts") or {},
        "analysis": doc.get("analysis") or {},
        "error": doc.get("error"),
    }
    if deliveries is not None:
        out["deliveries"] = [_public_delivery(d) for d in deliveries]
    return out


def _public_delivery(d: dict) -> dict:
    return {
        "id": d["_id"],
        "session_id": d.get("session_id"),
        "index": d.get("index"),
        "created_at": d.get("created_at"),
        "metrics": d.get("metrics"),
        "bounce": d.get("bounce"),
        "artifacts": d.get("artifacts") or {},
    }


@router.post("/sessions")
async def create_session(
    user: VerifiedUser,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    calibration: str = Form(...),
    title: str = Form("Ball Track session"),
    pitch_length_m: float | None = Form(None),
):
    if not file.filename:
        raise HTTPException(400, "Missing filename")
    suffix = Path(file.filename).suffix.lower() or ".mp4"
    if suffix not in {".mp4", ".mov", ".avi", ".mkv", ".webm"}:
        raise HTTPException(400, "Unsupported video type")
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
    dest = videos_dir / f"{session_id}{suffix}"
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    now = repo.utcnow()
    await repo.insert_session(
        {
            "_id": session_id,
            "user_id": user["_id"],
            "title": title.strip() or "Ball Track session",
            "path": str(dest),
            "original_name": file.filename,
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
    background_tasks.add_task(
        run_balltrack_job,
        job_id=job_id,
        session_id=session_id,
        video_path=dest,
        calibration=cal,
    )
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
    job["id"] = job.pop("_id")
    job["eta_seconds"] = await estimate_eta_seconds(
        collection="balltrack_jobs", pipeline="ballflight", job=job
    )
    return job


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
