from fastapi import APIRouter

from app.agent.ollama_agent import ollama_available
from app.config import get_settings
from app.db.mongo import ping_mongo

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    settings = get_settings()
    return {
        "ok": True,
        "service": "cric-lab",
        "app_env": settings.app_env,
        "llm": settings.llm_provider,
        "chat_model": settings.bedrock_model_id if settings.is_production else settings.ollama_model,
        "embed_model": settings.embed_model_id,
    }


@router.get("/health/mongo")
async def health_mongo():
    ok = await ping_mongo()
    return {"ok": ok, "db": get_settings().mongodb_db}


@router.get("/health/ollama")
async def health_ollama():
    status = await ollama_available()
    status.setdefault("provider", "ollama")
    status["app_env"] = get_settings().app_env
    return status


@router.get("/health/llm")
async def health_llm():
    """Active coaching LLM: local Ollama or Amazon Bedrock."""
    settings = get_settings()
    if settings.is_production:
        from app.agent.bedrock_client import bedrock_available

        return await bedrock_available()
    status = await ollama_available()
    status["provider"] = "ollama"
    status["app_env"] = settings.app_env
    return status
