import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    admin,
    assistant,
    auth,
    balltrack,
    bookings,
    coaching,
    health,
    notifications,
    support,
    training,
    videos,
)
from app.assistant.rag import build_index
from app.coaching.coaches_seed import seed_coaches
from app.config import get_settings
from app.db.indexes import ensure_indexes
from app.db.mongo import close_mongo, ping_mongo
from app.logging_config import configure_logging
from app.pipeline import quota


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    configure_logging(is_production=settings.is_production)
    for sub in ("videos", "artifacts", "frames", "balltrack"):
        (settings.storage_path / sub).mkdir(parents=True, exist_ok=True)
    # Idempotent; also the only place uniqueness and TTL rules are declared.
    # Ping once: if Mongo is down (typical on Vercel before Atlas is configured)
    # skip indexes/seed instead of waiting 8s per collection.
    if await ping_mongo():
        try:
            await ensure_indexes()
        except Exception as exc:
            logging.getLogger("criclab").warning(
                "mongo indexes skipped at startup: %s: %s", type(exc).__name__, exc
            )
        try:
            await seed_coaches()
        except Exception as exc:
            logging.getLogger("criclab").warning(
                "coach seed skipped at startup: %s: %s", type(exc).__name__, exc
            )
    else:
        logging.getLogger("criclab").warning(
            "mongo unreachable at startup; indexes and coach seed skipped"
        )
    log = logging.getLogger("criclab")
    if settings.is_production:
        log.info(
            "LLM: Bedrock production  chat=%s  embed=%s  region=%s",
            settings.bedrock_model_id,
            settings.bedrock_embedding_model_id,
            settings.aws_region,
        )
        if not (settings.aws_access_key_id and settings.aws_secret_access_key):
            log.warning(
                "APP_ENV=production but AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY are empty. "
                "Bedrock calls will fail unless an IAM role is attached to this host."
            )
    else:
        log.info(
            "LLM: Ollama local  chat=%s  embed=%s",
            settings.ollama_model,
            settings.ollama_embed_model,
        )
    # Embeds only what has changed; a no-op on most boots.
    try:
        await build_index()
    except Exception as exc:  # the assistant degrades, it does not block startup
        logging.getLogger("criclab").info(
            "assistant index not built at startup: %s", type(exc).__name__
        )
    if not settings.auth_configured:
        logging.getLogger("criclab").warning(
            "JWT_SECRET is not set — authentication endpoints will refuse to issue tokens."
        )
    dirty_task = asyncio.create_task(quota.recompute_loop(), name="quota-recompute")
    midnight_task = asyncio.create_task(quota.midnight_wake_loop(), name="quota-midnight-wake")
    yield
    for task in (dirty_task, midnight_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await close_mongo()


app = FastAPI(title="Cric-Lab API", version="0.1.0", lifespan=lifespan)
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_origin_regex=r"https://([a-z0-9-]+\.)?vercel\.app" if settings.is_production else None,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(notifications.router)
app.include_router(videos.router)
app.include_router(balltrack.router)
app.include_router(coaching.router)
app.include_router(bookings.router)
app.include_router(support.router)
app.include_router(training.router)
app.include_router(assistant.router)
app.include_router(admin.router)


@app.get("/")
async def root():
    return {"name": "Cric-Lab", "docs": "/docs"}
