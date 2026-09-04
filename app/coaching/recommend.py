"""Closed YouTube drill catalog for Train / admin HTTP. Never invent URLs.

Drill matching after pose / ball-flight lives in criclab-video-service
(`app.coaching.recommend`). This module only reads and writes drills.json.
"""

from __future__ import annotations

import json
import re
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

_CATALOG_PATH = Path(__file__).with_name("drills.json")
_CATALOG_LOCK = threading.Lock()
_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YT_FROM_URL = re.compile(
    r"(?:youtu\.be/|youtube(?:-nocookie)?\.com/(?:watch\?v=|embed/|shorts/|live/))([A-Za-z0-9_-]{11})"
)
_TAG_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")


def parse_youtube_id(raw: str) -> str:
    """Accept a watch URL or the 11-character id YouTube actually uses."""
    text = (raw or "").strip()
    match = _YT_FROM_URL.search(text)
    if match:
        return match.group(1)
    if "v=" in text:
        token = text.split("v=", 1)[1].split("&", 1)[0].strip()
        if _YT_ID.match(token):
            return token
    if _YT_ID.match(text):
        return text
    raise ValueError("Need a YouTube watch URL or an 11-character video id")


def slugify_drill_id(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (title or "").lower()).strip("_")[:40]
    return slug if _ID_RE.match(slug or "") else "drill"


def normalize_tags(tags: list[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags or []:
        tag = str(raw).strip().lower().replace(" ", "_")
        if not _TAG_RE.match(tag) or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return out


def save_catalog(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Persist the catalog and drop the in-process cache so the next read is fresh."""
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        did = str(item.get("id") or "").strip().lower()
        title = str(item.get("title") or "").strip()
        if not _ID_RE.match(did):
            raise ValueError("Each drill needs a lowercase id (letters, numbers, underscores)")
        if did in seen:
            raise ValueError(f"Duplicate drill id: {did}")
        if not title:
            raise ValueError("Each drill needs a title")
        seen.add(did)
        cleaned.append(
            {
                "id": did,
                "youtube_id": parse_youtube_id(str(item.get("youtube_id") or "")),
                "title": title,
                "tags": normalize_tags(item.get("tags")),
            }
        )
    payload = json.dumps(cleaned, indent=2) + "\n"
    tmp = _CATALOG_PATH.with_suffix(".json.tmp")
    with _CATALOG_LOCK:
        tmp.write_text(payload)
        tmp.replace(_CATALOG_PATH)
        load_catalog.cache_clear()
    return load_catalog()


@lru_cache(maxsize=1)
def load_catalog() -> list[dict[str, Any]]:
    with _CATALOG_PATH.open() as f:
        items = json.load(f)
    out = []
    for item in items:
        did = str(item.get("id") or "").strip()
        yt = str(item.get("youtube_id") or "").strip()
        if not did or not yt:
            continue
        out.append(
            {
                "id": did,
                "youtube_id": yt,
                "title": str(item.get("title") or did),
                "tags": [str(t) for t in (item.get("tags") or [])],
            }
        )
    return out
