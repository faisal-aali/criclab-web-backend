import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth, balltrack, coaching, health, notifications, videos
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


@app.get("/")
async def root():
    return {"name": "Cric-Lab", "docs": "/docs"}
