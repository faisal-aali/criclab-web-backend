from fastapi import APIRouter

from app.agent.ollama_agent import ollama_available
from app.config import get_settings
from app.db.mongo import ping_mongo

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    return {"ok": True, "service": "cric-lab"}


@router.get("/health/mongo")
async def health_mongo():
    ok = await ping_mongo()
    return {"ok": ok, "db": get_settings().mongodb_db}


@router.get("/health/ollama")
async def health_ollama():
    return await ollama_available()
