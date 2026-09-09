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


_VALID_STYLES = {"pace", "spin", "any"}


def normalize_styles(styles: list[str] | None) -> list[str]:
    """Keep only recognized style values; default to ["any"] (no style filtering)
    for a drill that isn't explicitly tagged pace/spin, rather than dropping it
    from every player's candidate list.
    """
    out = [s for s in (styles or []) if s in _VALID_STYLES]
    seen: set[str] = set()
    deduped = [s for s in out if not (s in seen or seen.add(s))]
    return deduped or ["any"]


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
                # Preserved from the existing catalog entry when present (e.g. editing
                # tags/title elsewhere in the list should not silently wipe a drill's
                # style); admin UI does not set this yet, so new/edited drills default
                # to "any" rather than being dropped from every candidate list.
                "styles": normalize_styles(item.get("styles")),
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
                "styles": [str(s) for s in (item.get("styles") or ["any"])] or ["any"],
            }
        )
    return out


def catalog_by_id() -> dict[str, dict[str, Any]]:
    return {d["id"]: d for d in load_catalog()}


def normalize_bowling_style(bowling_style: str | None) -> str | None:
    """Collapse free-text bowling_style into 'pace' or 'spin', or None if unclear.

    None means "don't filter" — we only exclude a drill when we're confident it
    targets the other style, never when the player's style is unknown.
    """
    if not bowling_style:
        return None
    s = str(bowling_style).strip().lower()
    if any(k in s for k in ("spin", "leg_break", "leg break", "off_break", "off break", "googly")):
        return "spin"
    if any(k in s for k in ("pace", "fast", "medium", "seam", "swing")):
        return "pace"
    return None


def _style_ok(drill: dict[str, Any], style: str | None) -> bool:
    if not style:
        return True
    styles = drill.get("styles") or ["any"]
    return "any" in styles or style in styles


def allowed_drill_summaries(
    tags: list[str] | None = None,
    *,
    bowling_style: str | None = None,
) -> list[dict[str, Any]]:
    """id / title / tags only — what the LLM is allowed to see for recommendations.

    Never hands the LLM a drill for the wrong bowling style when the style is known
    (e.g. a spin-technique drill for a pace bowler) — this is a deterministic filter,
    not something left to the model's judgement.
    """
    wanted = set(tags or [])
    style = normalize_bowling_style(bowling_style)
    catalog = load_catalog()

    def _summary(d: dict[str, Any]) -> dict[str, Any]:
        return {"id": d["id"], "title": d["title"], "tags": d["tags"]}

    rows = [_summary(d) for d in catalog if (not wanted or (wanted & set(d["tags"]))) and _style_ok(d, style)]
    if not rows:
        # Relax the tag filter before the style filter — style correctness matters more
        # than tag overlap, so we never fall all the way back to an unfiltered catalog
        # when the player's bowling style is known.
        rows = [_summary(d) for d in catalog if _style_ok(d, style)]
    if not rows:
        rows = [_summary(d) for d in catalog]
    return rows


_REASONS = {
    "front_leg_brace": "Front-leg brace scored low — work a block/plant drill.",
    "stride": "Stride length was outside a typical band — groove a repeatable approach.",
    "hip_shoulder": "Hip–shoulder separation was limited on this delivery.",
    "release_height": "Release looked low for this bowler — keep the wrist up through the crease.",
    "sequencing": "The measured sequence did not flow hip → trunk → arm.",
    "line": "Bounce was off the intended line — target off stump.",
    "length": "Length was too full or too short for a stock ball.",
    "arm_speed": "Arm speed at leave-hand was below the heuristic band.",
    "side_on_setup": "This camera angle cannot support truthful speed — re-film side-on.",
}


def fallback_picks(
    tags: list[str],
    *,
    limit: int = 3,
    bowling_style: str | None = None,
) -> list[dict[str, Any]]:
    """Deterministic catalog picks when the LLM output is empty or invalid."""
    catalog = load_catalog()
    style = normalize_bowling_style(bowling_style)
    candidates = [d for d in catalog if _style_ok(d, style)] or catalog
    by_id = catalog_by_id()
    picked: list[str] = []
    for tag in tags:
        for d in candidates:
            if tag in d["tags"] and d["id"] not in picked:
                picked.append(d["id"])
                break
        if len(picked) >= limit:
            break
    if not picked:
        picked = [d["id"] for d in candidates[:limit]]
    out = []
    for i, did in enumerate(picked[:limit]):
        d = by_id.get(did)
        if not d:
            continue
        tag = next((t for t in d["tags"] if t in tags), (d["tags"][0] if d["tags"] else ""))
        out.append(
            {
                "drill_id": d["id"],
                "youtube_id": d["youtube_id"],
                "title": d["title"],
                "tags": d["tags"],
                "reason": _REASONS.get(tag, "Selected from the Cric-Lab drill catalog."),
                "priority": i + 1,
                "source": "fallback",
            }
        )
    return out


def hydrate_recommendations(
    picks: list[dict[str, Any]] | None,
    *,
    tags: list[str],
    bowling_style: str | None = None,
) -> list[dict[str, Any]]:
    """Keep only catalog IDs. Drop any invented drill_id values and any pick that
    targets the wrong bowling style — a safety net in case the LLM ignores the
    style instruction in the prompt.
    """
    by_id = catalog_by_id()
    style = normalize_bowling_style(bowling_style)
    hydrated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in picks or []:
        did = str(raw.get("drill_id") or raw.get("id") or "").strip()
        if did not in by_id or did in seen:
            continue
        d = by_id[did]
        if not _style_ok(d, style):
            continue
        try:
            prio = int(raw.get("priority") or (len(hydrated) + 1))
        except (TypeError, ValueError):
            prio = len(hydrated) + 1
        reason = str(raw.get("reason") or "").strip() or "Catalog drill matched to a measured weakness."
        hydrated.append(
            {
                "drill_id": d["id"],
                "youtube_id": d["youtube_id"],
                "title": d["title"],
                "tags": d["tags"],
                "reason": reason,
                "priority": prio,
                "source": "gemma",
            }
        )
        seen.add(did)
    if not hydrated:
        return fallback_picks(tags, bowling_style=bowling_style)
    hydrated.sort(key=lambda r: r.get("priority") or 99)
    return hydrated[:5]
