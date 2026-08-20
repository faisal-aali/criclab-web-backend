from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import balltrack, coaching, health, videos
from app.config import get_settings
from app.db.mongo import close_mongo


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    for sub in ("videos", "artifacts", "frames", "balltrack"):
        (settings.storage_path / sub).mkdir(parents=True, exist_ok=True)
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
app.include_router(videos.router)
app.include_router(balltrack.router)
app.include_router(coaching.router)


@app.get("/")
async def root():
    return {"name": "Cric-Lab", "docs": "/docs"}
