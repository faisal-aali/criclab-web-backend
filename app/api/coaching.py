from __future__ import annotations

from fastapi import APIRouter, Query

from app.coaching.recommend import load_catalog

router = APIRouter(prefix="/coaching", tags=["coaching"])


@router.get("/drills")
def list_drills(tag: str | None = Query(None)):
    items = load_catalog()
    if tag:
        wanted = tag.strip().lower()
        items = [d for d in items if wanted in [t.lower() for t in d.get("tags") or []]]
    return {"items": items, "tags": sorted({t for d in load_catalog() for t in d.get("tags") or []})}
