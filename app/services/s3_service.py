"""S3 presigned PUT (browser upload) and CloudFront signed GET (playback).

Mongo stores object keys. This module mints URLs at request time. Never persist
a signed URL — they expire. Historic Cloudinary HTTPS links still play.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse
from uuid import uuid4

from app.config import get_settings

PREFIXES = ("original/", "compressed/", "overlays/", "files/")
_VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
_GENERIC_CONTENT_TYPES = frozenset(
    {"", "application/octet-stream", "binary/octet-stream"}
)
_SUFFIX_CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
PUT_EXPIRES = 15 * 60
GET_EXPIRES = 60 * 60
PLAYBACK_TRANSFORMATION = "f_mp4,vc_h264"


def s3_endpoint_url(region: str | None) -> str | None:
    """Regional S3 API host. Global s3.amazonaws.com 301s ap-south-1 buckets."""
    r = (region or "").strip()
    if not r:
        return None
    return f"https://s3.{r}.amazonaws.com"


def _s3_client():
    import boto3
    from botocore.config import Config

    settings = get_settings()
    region = (settings.s3_region or settings.aws_region or "ap-south-1").strip()
    kwargs: dict[str, Any] = {
        "region_name": region,
        # when_supported (boto3 1.36+) signs checksum headers the browser never sends → 403.
        "config": Config(
            signature_version="s3v4",
            s3={"addressing_style": "virtual"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    }
    endpoint = s3_endpoint_url(region)
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        kwargs["aws_access_key_id"] = settings.aws_access_key_id
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
    return boto3.client("s3", **kwargs)


def s3_configured() -> bool:
    settings = get_settings()
    return bool(settings.s3_bucket and settings.s3_region)


def cloudfront_signing_configured() -> bool:
    settings = get_settings()
    return bool(
        settings.cloudfront_domain
        and settings.cloudfront_key_pair_id
        and settings.cloudfront_private_key
    )


def is_our_object_key(key: str | None) -> bool:
    k = (key or "").strip().lstrip("/")
    if not k or ".." in k or "\\" in k or "\n" in k:
        return False
    return any(k.startswith(p) for p in PREFIXES)


def resolve_content_type(name_or_key: str, hint: str | None = None) -> str:
    """Pick a MIME type from a filename/object key, with an optional browser hint."""
    hinted = (hint or "").strip().lower()
    if hinted and hinted not in _GENERIC_CONTENT_TYPES:
        return hinted
    guessed, _ = mimetypes.guess_type(name_or_key)
    if guessed:
        return guessed
    suffix = Path(name_or_key).suffix.lower()
    return _SUFFIX_CONTENT_TYPES.get(suffix, "application/octet-stream")


def original_key(user_id: str, filename: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in _VIDEO_SUFFIXES:
        suffix = ".mp4"
    uid = str(user_id).replace("/", "").replace("\\", "")
    return f"original/{uid}/{uuid4().hex[:16]}{suffix}"


def incoming_original_key(value: str | None) -> str | None:
    """Accept `original/...` or a virtual-hosted S3 URL. Reject CloudFront / foreign hosts."""
    raw = (value or "").strip()
    if not raw:
        return None
    stripped = raw.lstrip("/")
    if is_our_object_key(stripped) and stripped.startswith("original/"):
        return stripped
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    cf = _cloudfront_domain().lower()
    if "cloudfront.net" in host or (cf and host == cf):
        return None
    path = parsed.path.lstrip("/")
    if parsed.scheme == "s3":
        return path if is_our_object_key(path) and path.startswith("original/") else None
    if "amazonaws.com" not in host:
        return None
    settings = get_settings()
    bucket = (settings.s3_bucket or "").strip().lower()
    if host.startswith("s3.") or host.startswith("s3-"):
        parts = path.split("/", 1)
        if len(parts) != 2:
            return None
        url_bucket, path = parts[0].lower(), parts[1]
        if bucket and url_bucket != bucket:
            return None
    elif bucket and not host.startswith(f"{bucket}.s3"):
        return None
    if is_our_object_key(path) and path.startswith("original/"):
        return path
    return None


def _aws_v4_signing_key(secret: str, datestamp: str, region: str) -> bytes:
    k_date = hmac.new(("AWS4" + secret).encode("utf-8"), datestamp.encode("ascii"), hashlib.sha256).digest()
    k_region = hmac.new(k_date, region.encode("ascii"), hashlib.sha256).digest()
    k_service = hmac.new(k_region, b"s3", hashlib.sha256).digest()
    return hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()


def _presign_put_url(key: str, content_type: str) -> str | None:
    """SigV4 query PUT that browsers can use.

    boto3's generate_presigned_url puts UNSIGNED-PAYLOAD in the canonical
    request but not in the query string. S3 then hashes the file body and
    returns SignatureDoesNotMatch. This signer adds X-Amz-Content-Sha256 to
    the signed query and signs ``host`` plus ``content-type``.
    """
    settings = get_settings()
    bucket = (settings.s3_bucket or "").strip()
    region = (settings.s3_region or "ap-south-1").strip()
    access = (settings.aws_access_key_id or "").strip()
    secret = (settings.aws_secret_access_key or "").strip()
    if not (bucket and region and access and secret):
        return None
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    host = f"{bucket}.s3.{region}.amazonaws.com"
    canonical_uri = "/" + quote(key, safe="/")
    credential_scope = f"{datestamp}/{region}/s3/aws4_request"
    query = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Content-Sha256": "UNSIGNED-PAYLOAD",
        "X-Amz-Credential": f"{access}/{credential_scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(PUT_EXPIRES),
        "X-Amz-SignedHeaders": "content-type;host",
    }
    canonical_query = "&".join(
        f"{quote(name, safe='-_.~')}={quote(value, safe='-_.~')}"
        for name, value in sorted(query.items())
    )
    canonical_request = (
        f"PUT\n{canonical_uri}\n{canonical_query}\n"
        f"content-type:{content_type}\nhost:{host}\n\ncontent-type;host\nUNSIGNED-PAYLOAD"
    )
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )
    signature = hmac.new(
        _aws_v4_signing_key(secret, datestamp, region),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"https://{host}{canonical_uri}?{canonical_query}&X-Amz-Signature={signature}"


def presigned_put(key: str, content_type: str) -> dict[str, Any] | None:
    """Browser PUT of the original clip. Send only the signed Content-Type header."""
    if not s3_configured() or not is_our_object_key(key):
        return None
    resolved = resolve_content_type(key, content_type)
    url = _presign_put_url(key, resolved)
    if not url:
        return None
    return {
        "upload_url": url,
        "method": "PUT",
        "headers": {"Content-Type": resolved},
        "key": key,
    }


def pem_bytes(raw: str) -> bytes:
    text = (raw or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\r\n", "\n")
    return text.encode("ascii")


@lru_cache(maxsize=4)
def _private_key(pem: bytes):
    from cryptography.hazmat.primitives import serialization

    return serialization.load_pem_private_key(pem, password=None)


def _cloudfront_domain() -> str:
    raw = (get_settings().cloudfront_domain or "").strip()
    raw = raw.removeprefix("https://").removeprefix("http://").strip().strip("/")
    return raw


def signed_get(key: str | None, *, expires: int = GET_EXPIRES) -> str | None:
    """CloudFront canned-policy signed GET. None if signing is off or key is bad."""
    k = (key or "").strip().lstrip("/")
    if not is_our_object_key(k) or not cloudfront_signing_configured():
        return None
    settings = get_settings()
    domain = _cloudfront_domain()
    if not domain:
        return None
    resource = f"https://{domain}/{quote(k, safe='/')}"
    expire_at = int(time.time()) + max(60, int(expires))
    policy = json.dumps(
        {
            "Statement": [
                {
                    "Resource": resource,
                    "Condition": {"DateLessThan": {"AWS:EpochTime": expire_at}},
                }
            ]
        },
        separators=(",", ":"),
    )
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    signature = _private_key(pem_bytes(settings.cloudfront_private_key)).sign(
        policy.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA1(),
    )
    encoded = (
        base64.b64encode(signature)
        .decode("ascii")
        .replace("+", "-")
        .replace("=", "_")
        .replace("/", "~")
    )
    return (
        f"{resource}?Expires={expire_at}&Signature={encoded}"
        f"&Key-Pair-Id={settings.cloudfront_key_pair_id}"
    )


def is_cloudinary_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "res.cloudinary.com" or host.endswith(".cloudinary.com")


def legacy_cloudinary_playback_url(url: str) -> str:
    """H.264 MP4 derivative of an old Cloudinary clip so Chrome can play HEVC .mov."""
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
    path = parsed.path[: idx + len(marker)] + f"{PLAYBACK_TRANSFORMATION}/{rest}"
    if path.lower().endswith(".mov"):
        path = path[:-4] + ".mp4"
    return parsed._replace(path=path).geturl()
