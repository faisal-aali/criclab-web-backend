from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_storage_dir() -> Path:
    """Runtime uploads/artifacts live outside the git repo (OS user data dir)."""
    return Path.home() / ".local" / "share" / "criclab"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_db: str = "criclab"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "gemma3:4b"
    ollama_embed_model: str = "nomic-embed-text"
    # Leave empty to use ~/.local/share/criclab (outside the repo).
    storage_dir: str = ""
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    default_meters_per_pixel: float | None = None

    # Cloudinary (processed video + PDF hosting)
    cloudinary_url: str | None = None
    cloudinary_cloud_name: str | None = None
    cloudinary_api_key: str | None = None
    cloudinary_api_secret: str | None = None

    @field_validator(
        "default_meters_per_pixel",
        "cloudinary_url",
        "cloudinary_cloud_name",
        "cloudinary_api_key",
        "cloudinary_api_secret",
        mode="before",
    )
    @classmethod
    def empty_str_to_none(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @property
    def storage_path(self) -> Path:
        raw = (self.storage_dir or "").strip()
        if raw:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = (Path(__file__).resolve().parent.parent / path).resolve()
        else:
            path = default_storage_dir()
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def cloudinary_configured(self) -> bool:
        if self.cloudinary_url:
            return True
        return bool(self.cloudinary_cloud_name and self.cloudinary_api_key and self.cloudinary_api_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
