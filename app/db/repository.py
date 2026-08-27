from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.db.mongo import get_db


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def insert_video(doc: dict[str, Any]) -> str:
    db = get_db()
    await db.videos.insert_one(doc)
    return doc["_id"]


async def insert_job(doc: dict[str, Any]) -> str:
    db = get_db()
    await db.jobs.insert_one(doc)
    return doc["_id"]


async def update_job(job_id: str, **fields: Any) -> None:
    db = get_db()
    fields["updated_at"] = utcnow()
    await db.jobs.update_one({"_id": job_id}, {"$set": fields})


async def get_job(job_id: str) -> dict[str, Any] | None:
    return await get_db().jobs.find_one({"_id": job_id})


async def get_delivery(delivery_id: str) -> dict[str, Any] | None:
    return await get_db().deliveries.find_one({"_id": delivery_id})


async def insert_delivery(doc: dict[str, Any]) -> str:
    await get_db().deliveries.insert_one(doc)
    return doc["_id"]


async def list_deliveries(
    limit: int = 50, player_name: str | None = None, user_id: str | None = None
) -> list[dict[str, Any]]:
    query: dict[str, Any] = {}
    if player_name:
        query["player_name"] = player_name
    if user_id:
        query["user_id"] = user_id
    cursor = get_db().deliveries.find(query).sort("created_at", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def top_throws(limit: int = 20) -> list[dict[str, Any]]:
    """Fastest Action deliveries with a measured (not estimated) ball speed."""
    pipeline: list[dict[str, Any]] = [
        {
            "$match": {
                "metrics.ball_speed_kmh.status": "ok",
                "metrics.ball_speed_kmh.value": {"$gt": 0},
            }
        },
        {"$sort": {"metrics.ball_speed_kmh.value": -1, "created_at": 1}},
        {"$limit": limit},
        {
            "$project": {
                "_id": 1,
                "user_id": 1,
                "player_name": 1,
                "created_at": 1,
                "metrics.ball_speed_kmh": 1,
                "metrics.arm_speed_kmh": 1,
                "metrics.delivery_type": 1,
                "metrics.player_profile": 1,
            }
        },
    ]
    return await get_db().deliveries.aggregate(pipeline).to_list(limit)


async def get_video(video_id: str) -> dict[str, Any] | None:
    return await get_db().videos.find_one({"_id": video_id})
