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
    videos,
)
from app.assistant.rag import build_index
from app.coaching.coaches_seed import seed_coaches
from app.config import get_settings
from app.db.indexes import ensure_indexes
from app.db.mongo import close_mongo


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    for sub in ("videos", "artifacts", "frames", "balltrack"):
        (settings.storage_path / sub).mkdir(parents=True, exist_ok=True)
    # Idempotent; also the only place uniqueness and TTL rules are declared.
    await ensure_indexes()
    # A fresh install should have a working coaching calendar, not an empty
    # page. Existing profiles are never touched.
    await seed_coaches()
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
    yield
    await close_mongo()


app = FastAPI(title="Cric-Lab API", version="0.1.0", lifespan=lifespan)
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
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
app.include_router(assistant.router)
app.include_router(admin.router)


@app.get("/")
async def root():
    return {"name": "Cric-Lab", "docs": "/docs"}
