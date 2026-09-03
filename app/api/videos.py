from __future__ import annotations

import shutil
from pathlib import Path

from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.api.deps import CurrentUser, VerifiedUser, visible_to
from app.config import get_settings
from app.db import repository as repo
from app.pipeline import quota
from app.pipeline.eta import estimate_eta_seconds
from app.pipeline.profile import parse_player_profile
from app.services import s3_service

router = APIRouter(tags=["analysis"])

_VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".webm"}


@router.get("/videos/upload-params")
async def video_upload_params(
    user: VerifiedUser,
    filename: str = "",
    content_type: str = "video/mp4",
):
    """S3 presigned PUT so the browser can upload the clip off this API."""
    if not s3_service.s3_configured():
        return {"configured": False}
    key = s3_service.original_key(user["_id"], filename)
    params = s3_service.presigned_put(key, content_type)
    if not params:
        return {"configured": False}
    return {"configured": True, **params}


def _incoming_source_key(source_key: str | None, source_url: str | None) -> str | None:
    if (source_key or "").strip():
        key = s3_service.incoming_original_key(source_key)
        if not key:
            raise HTTPException(400, "Video key is not from our upload host")
        return key
    url = (source_url or "").strip()
    if not url:
        return None
    key = s3_service.incoming_original_key(url)
    if not key:
        raise HTTPException(400, "Video URL is not from our upload host")
    return key


async def _store_incoming_video(
    *,
    dest: Path,
    file: UploadFile | None,
    source_key: str | None,
    source_url: str | None,
    original_name: str | None,
) -> tuple[Path, str, str | None]:
    """Write a local upload now, or record an S3 object key for the worker.

    Fetching the remote clip inside this request blocked POST /videos — the UI
    sat on "Uploading…" with no job to poll. The worker downloads by key.
    """
    key = _incoming_source_key(source_key, source_url)
    if key:
        suffix = Path(key).suffix.lower() or ".mp4"
        if suffix not in _VIDEO_SUFFIXES:
            suffix = ".mp4"
        dest = dest.with_suffix(suffix)
        name = original_name or Path(key).name or dest.name
        return dest, name, key

    if not file or not file.filename:
        raise HTTPException(400, "Attach a video or a source_key")
    suffix = Path(file.filename).suffix.lower() or ".mp4"
    if suffix not in _VIDEO_SUFFIXES:
        raise HTTPException(400, "Unsupported video type")
    dest = dest.with_suffix(suffix)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    return dest, file.filename, None


@router.post("/videos")
async def upload_video(
    user: VerifiedUser,
    file: UploadFile | None = File(None),
    source_key: str | None = Form(None),
    source_url: str | None = Form(None),
    original_name: str | None = Form(None),
    player_name: str = Form("Bowler"),
    first_name: str | None = Form(None),
    last_name: str | None = Form(None),
    date_of_birth: str | None = Form(None),
    height_ft: float | None = Form(None),
    height_in: float | None = Form(None),
    weight_lbs: float | None = Form(None),
    bowling_arm: str | None = Form(None),
    bowling_style: str | None = Form(None),
    meters_per_pixel: float | None = Form(None),
    reference_height_m: float | None = Form(None),
):

    profile = parse_player_profile(
        player_name=player_name,
        first_name=first_name,
        last_name=last_name,
        date_of_birth=date_of_birth,
        height_ft=height_ft,
        height_in=height_in,
        reference_height_m=reference_height_m,
        weight_lbs=weight_lbs,
        bowling_arm=bowling_arm,
        bowling_style=bowling_style,
        meters_per_pixel=meters_per_pixel,
    )

    settings = get_settings()
    videos_dir = settings.storage_path / "videos"
    videos_dir.mkdir(parents=True, exist_ok=True)

    video_id = repo.new_id("vid")
    job_id = repo.new_id("job")
    dest, stored_name, incoming_key = await _store_incoming_video(
        dest=videos_dir / video_id,
        file=file,
        source_key=source_key,
        source_url=source_url,
        original_name=original_name,
    )

    video_doc = {
        "_id": video_id,
        "user_id": user["_id"],
        "original_name": stored_name,
        "path": str(dest),
        "source_key": incoming_key,
        "source_url": None,
        "compressed_key": None,
        "content_type": (file.content_type if file else "video/mp4"),
        "player_name": profile["player_name"],
        "player_profile": profile,
        "created_at": repo.utcnow(),
    }
    await repo.insert_video(video_doc)

    job_doc = {
        "_id": job_id,
        "video_id": video_id,
        "user_id": user["_id"],
        "status": "queued",
        "progress": 0,
        "stage": "queued",
        "message": "Queued for analysis",
        "player_name": profile["player_name"],
        "created_at": repo.utcnow(),
        "updated_at": repo.utcnow(),
    }
    await repo.insert_job(job_doc)
    # criclab-video-service workers claim queued jobs. This API does not run CV.
    await quota.schedule_queued_jobs(notify_inserted_id=job_id)

    return {"video_id": video_id, "job_id": job_id, "status": "queued"}


def _public_job(job: dict[str, Any], *, kind: str, eta: int | None) -> dict[str, Any]:
    out = dict(job)
    out["id"] = out.pop("_id")
    out["kind"] = kind
    out["eta_seconds"] = eta
    return out


@router.get("/jobs/active")
async def list_active_jobs(user: CurrentUser):
    """In-flight Action + Ball flight jobs for the header progress icon."""
    from app.balltrack import repo as bt_repo

    await quota.fail_stale_in_flight_jobs()
    action = await repo.list_active_jobs(user["_id"])
    flight = await bt_repo.list_active_jobs(user["_id"])
    items = []
    for job in action:
        eta = await estimate_eta_seconds(collection="jobs", pipeline="action", job=job)
        items.append(_public_job(job, kind="action", eta=eta))
    for job in flight:
        eta = await estimate_eta_seconds(
            collection="balltrack_jobs", pipeline="ballflight", job=job
        )
        items.append(_public_job(job, kind="ballflight", eta=eta))
    items.sort(key=lambda j: str(j.get("created_at") or ""), reverse=True)
    return {"items": items}


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, user: CurrentUser):
    job = await repo.get_job(job_id)
    if not visible_to(user, (job or {}).get("user_id"), exists=bool(job)):
        raise HTTPException(404, "Job not found")
    if job.get("status") not in ("queued", "claimed", "processing", "analyzing"):
        raise HTTPException(409, "This clip has already finished")
    updated = await quota.cancel_queued_job(job_id=job_id, collection="jobs")
    if not updated:
        raise HTTPException(409, "This clip has already finished")
    return {"id": updated["_id"], "status": updated.get("status")}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, user: CurrentUser):
    job = await repo.get_job(job_id)
    # Same 404 whether the job doesn't exist or belongs to someone else — no
    # signal to a caller probing ids that a given job exists at all. Staff
    # may open any job so the admin panel can show the same results page.
    if not visible_to(user, (job or {}).get("user_id"), exists=bool(job)):
        raise HTTPException(404, "Job not found")
    job = await quota.fail_stale_job(job, collection="jobs")
    job["id"] = job.pop("_id")
    job["eta_seconds"] = await estimate_eta_seconds(collection="jobs", pipeline="action", job=job)
    return job


def _https_or_local(signed: str | None, legacy: str | None, local: str | None) -> str | None:
    return signed or legacy or local


def _delivery_artifacts(d: dict[str, Any], video: dict[str, Any] | None = None) -> dict[str, Any]:
    job_id = d.get("job_id")
    art = d.get("artifacts") or {}
    overlay = _https_or_local(
        s3_service.signed_get(art.get("overlay_key")),
        art.get("cloudinary_video_url"),
        f"/artifacts/{job_id}/overlay.mp4" if job_id else None,
    )
    pdf = _https_or_local(
        s3_service.signed_get(art.get("pdf_key")),
        art.get("cloudinary_pdf_url"),
        f"/artifacts/{job_id}/bowling_report.pdf" if job_id else None,
    )
    still = _https_or_local(
        s3_service.signed_get(art.get("release_still_key")),
        None,
        f"/artifacts/{job_id}/release.jpg" if job_id else None,
    )
    compressed = s3_service.signed_get((video or {}).get("compressed_key") or art.get("compressed_key"))
    return {
        "pdf_url": pdf,
        "overlay_video_url": overlay,
        "release_still_url": still,
        "original_video_url": _original_video_url(video) if video is not None else compressed,
        "compressed_video_url": compressed,
        "cloudinary_video_url": overlay,
        "cloudinary_pdf_url": pdf,
    }


def _original_video_url(video: dict[str, Any] | None) -> str | None:
    """Before · your clip: CloudFront-signed compressed H.264, else a local file.

    Never return the raw ``original/`` object (often an iPhone HEVC ``.mov``).
    Historic Cloudinary ``source_url`` rows still play via the old transform.
    """
    if not video:
        return None
    signed = s3_service.signed_get(video.get("compressed_key"))
    if signed:
        return signed
    src = (video.get("source_url") or "").strip()
    if src and s3_service.is_cloudinary_url(src):
        return s3_service.legacy_cloudinary_playback_url(src)
    name = Path(video["path"]).name if video.get("path") else None
    if not name:
        return None
    local = get_settings().storage_path / "videos" / name
    if not local.is_file():
        return None
    return f"/media/videos/{name}"


def _ok_metric_value(metrics: dict[str, Any] | None, key: str) -> float | None:
    node = (metrics or {}).get(key) or {}
    if not isinstance(node, dict) or node.get("status") != "ok":
        return None
    val = node.get("value")
    return float(val) if isinstance(val, (int, float)) else None


@router.get("/leaderboard")
async def leaderboard(user: CurrentUser, limit: int = 20):
    """Top Action throws by measured ball speed. Other players' reports stay private."""
    cap = min(max(limit, 1), 20)
    rows = await repo.top_throws(limit=cap)
    items = []
    for rank, d in enumerate(rows, start=1):
        metrics = d.get("metrics") or {}
        dtype = metrics.get("delivery_type") or {}
        profile = metrics.get("player_profile") or {}
        pace = dtype.get("value") if isinstance(dtype, dict) and dtype.get("status") == "ok" else None
        mine = d.get("user_id") == user["_id"]
        items.append(
            {
                "rank": rank,
                "player_name": d.get("player_name") or "Bowler",
                "ball_speed_kmh": _ok_metric_value(metrics, "ball_speed_kmh"),
                "arm_speed_kmh": _ok_metric_value(metrics, "arm_speed_kmh"),
                "delivery_type": pace,
                "bowling_arm": profile.get("bowling_arm") if isinstance(profile, dict) else None,
                "created_at": d.get("created_at"),
                "mine": mine,
                "result_id": d["_id"] if mine else None,
            }
        )
    return {"items": items}


@router.get("/deliveries")
async def list_deliveries(user: CurrentUser, limit: int = 50):
    items = await repo.list_deliveries(limit=limit, user_id=user["_id"])
    out = []
    for d in items:
        out.append(
            {
                "id": d["_id"],
                "job_id": d.get("job_id"),
                "video_id": d.get("video_id"),
                "player_name": d.get("player_name"),
                "created_at": d.get("created_at"),
                "metrics": d.get("metrics"),
                "analysis_summary": (d.get("analysis") or {}).get("summary"),
                "cloudinary": d.get("cloudinary"),
                "artifacts": _delivery_artifacts(d),
            }
        )
    return {"items": out}


@router.get("/deliveries/{delivery_id}")
async def get_delivery(delivery_id: str, user: CurrentUser):
    d = await repo.get_delivery(delivery_id)
    if not visible_to(user, (d or {}).get("user_id"), exists=bool(d)):
        raise HTTPException(404, "Delivery not found")
    video = await repo.get_video(d["video_id"]) if d.get("video_id") else None
    return {
        "id": d["_id"],
        "job_id": d.get("job_id"),
        "video_id": d.get("video_id"),
        "player_name": d.get("player_name"),
        "player_profile": d.get("player_profile") or (d.get("metrics") or {}).get("player_profile"),
        "created_at": d.get("created_at"),
        "meta": d.get("meta"),
        "metrics": d.get("metrics"),
        "analysis": d.get("analysis"),
        "action": d.get("action"),
        "release": d.get("release"),
        "cloudinary": d.get("cloudinary"),
        "artifacts": _delivery_artifacts(d, video),
    }


@router.get("/artifacts/{job_id}/{filename}")
async def get_artifact(job_id: str, filename: str, download: bool = False):
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "Invalid filename")
    path = get_settings().storage_path / "artifacts" / job_id / filename
    if not path.exists():
        raise HTTPException(404, "Artifact not found")
    if download:
        # Force a save dialog (Content-Disposition: attachment) instead of inline view.
        return FileResponse(path, filename=filename)
    return FileResponse(path)


@router.get("/media/videos/{filename}")
async def get_video_media(filename: str):
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "Invalid filename")
    path = get_settings().storage_path / "videos" / filename
    if not path.exists():
        raise HTTPException(404, "Video not found")
    return FileResponse(path)
