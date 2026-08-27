"""Cloudinary upload for processed videos and PDF reports.

If Cloudinary is not configured (no creds in .env) every call returns None and
the app falls back to serving artifacts from local disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.config import get_settings

try:
    import cloudinary
    import cloudinary.uploader
    _CLOUDINARY_IMPORTED = True
except Exception:  # pragma: no cover
    cloudinary = None
    _CLOUDINARY_IMPORTED = False

_configured = False


def _ensure_configured() -> bool:
    global _configured
    if not _CLOUDINARY_IMPORTED:
        return False
    settings = get_settings()
    if not settings.cloudinary_configured:
        return False
    if _configured:
        return True
    cloud_name = settings.cloudinary_cloud_name
    api_key = settings.cloudinary_api_key
    api_secret = settings.cloudinary_api_secret

    # Fill any missing explicit field by parsing CLOUDINARY_URL
    # (cloudinary://<api_key>:<api_secret>@<cloud_name>).
    if settings.cloudinary_url and not (cloud_name and api_key and api_secret):
        parsed = urlparse(settings.cloudinary_url)
        api_key = api_key or parsed.username
        api_secret = api_secret or parsed.password
        cloud_name = cloud_name or parsed.hostname

    if not (cloud_name and api_key and api_secret):
        return False

    cloudinary.config(
        cloud_name=cloud_name,
        api_key=api_key,
        api_secret=api_secret,
        secure=True,
    )
    _configured = True
    return True


def is_configured() -> bool:
    return _ensure_configured()


def upload_video(path: Path, public_id: str, folder: str = "criclab/videos") -> dict[str, Any] | None:
    if not _ensure_configured():
        return None
    res = cloudinary.uploader.upload_large(
        str(path),
        resource_type="video",
        public_id=public_id,
        folder=folder,
        overwrite=True,
        # Deliver a browser-friendly H.264 MP4 regardless of the source codec.
        eager=[{"format": "mp4", "video_codec": "h264", "quality": "auto"}],
        eager_async=False,
    )
    return _video_result(res)


def upload_pdf(path: Path, public_id: str, folder: str = "criclab/reports") -> dict[str, Any] | None:
    if not _ensure_configured():
        return None
    res = cloudinary.uploader.upload(
        str(path),
        resource_type="raw",
        public_id=f"{public_id}.pdf",
        folder=folder,
        overwrite=True,
    )
    return {"secure_url": res.get("secure_url"), "public_id": res.get("public_id"), "bytes": res.get("bytes")}


def _video_result(res: dict[str, Any]) -> dict[str, Any]:
    secure = res.get("secure_url")
    playback = secure
    eager = res.get("eager") or []
    if eager and eager[0].get("secure_url"):
        playback = eager[0]["secure_url"]
    return {
        "secure_url": secure,
        "playback_url": playback,
        "public_id": res.get("public_id"),
        "duration": res.get("duration"),
        "bytes": res.get("bytes"),
        "width": res.get("width"),
        "height": res.get("height"),
    }
