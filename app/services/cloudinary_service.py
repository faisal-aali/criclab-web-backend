"""Cloudinary signed upload so the browser can POST clips without sending them through this API."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from app.config import get_settings

try:
    import cloudinary
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


def signed_video_upload_params(*, folder: str = "criclab/incoming") -> dict[str, Any] | None:
    """Browser uploads the clip to Cloudinary so this API never sees the bytes."""
    if not _ensure_configured():
        return None
    import time

    from cloudinary.utils import api_sign_request

    timestamp = int(time.time())
    params = {"timestamp": timestamp, "folder": folder}
    signature = api_sign_request(params, cloudinary.config().api_secret)
    return {
        "cloud_name": cloudinary.config().cloud_name,
        "api_key": cloudinary.config().api_key,
        "timestamp": timestamp,
        "signature": signature,
        "folder": folder,
        "resource_type": "video",
    }


def is_cloudinary_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "res.cloudinary.com" or host.endswith(".cloudinary.com")


def browser_playback_url(url: str) -> str:
    """H.264 MP4 derivative of an incoming Cloudinary clip for ``<video>``.

    Browser uploads keep the phone container (often ``.mov``). The overlay is
    re-encoded; this original URL is not. Insert ``f_mp4,vc_h264`` so the
    results page can play the same file the worker analysed.
    """
    if not is_cloudinary_url(url):
        return url
    parsed = urlparse(url)
    marker = "/video/upload/"
    idx = parsed.path.find(marker)
    if idx < 0:
        return url
    rest = parsed.path[idx + len(marker) :]
    first = rest.split("/", 1)[0]
    if first and ("f_mp4" in first or "vc_h264" in first):
        return url
    path = parsed.path[: idx + len(marker)] + f"f_mp4,vc_h264/{rest}"
    if path.lower().endswith(".mov"):
        path = path[:-4] + ".mp4"
    return parsed._replace(path=path).geturl()
