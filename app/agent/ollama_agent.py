"""Ollama / Bedrock — chat assistant only. Video coaching notes live in criclab-video-service."""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.config import get_settings


def _model_installed(configured: str, installed: list[str]) -> bool:
    """Match Ollama tag names with or without the :latest suffix."""
    if configured in installed:
        return True
    base = configured.split(":", 1)[0]
    return any(m == base or m.startswith(f"{base}:") for m in installed)


async def ollama_available() -> dict[str, Any]:
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{settings.ollama_base_url}/api/tags")
            r.raise_for_status()
            models = [m.get("name", "") for m in r.json().get("models", [])]
            has_model = _model_installed(settings.ollama_model, models)
            has_nomic = _model_installed(settings.ollama_embed_model, models)
            return {
                "ok": has_model,
                "configured_model": settings.ollama_model,
                "configured_embed_model": settings.ollama_embed_model,
                "models": models,
                "has_model": has_model,
                "has_nomic": has_nomic,
            }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "configured_model": settings.ollama_model,
            "models": [],
            "has_model": False,
            "has_nomic": False,
        }


async def generate_text(
    prompt: str,
    system: str | None = None,
    *,
    num_predict: int = 500,
    timeout_s: float = 300.0,
) -> str:
    settings = get_settings()
    if settings.llm_provider == "bedrock":
        from app.agent.bedrock_client import converse

        return await converse(prompt, system=system, max_tokens=num_predict, timeout_s=timeout_s)
    payload: dict[str, Any] = {
        "model": settings.ollama_model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": "10m",
        "options": {"temperature": 0.3, "num_predict": num_predict},
    }
    if system:
        payload["system"] = system
    timeout = httpx.Timeout(timeout_s, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(f"{settings.ollama_base_url}/api/generate", json=payload)
        r.raise_for_status()
        return (r.json().get("response") or "").strip()


async def generate_text_stream(
    prompt: str,
    system: str | None = None,
    *,
    num_predict: int = 500,
    timeout_s: float = 60.0,
):
    settings = get_settings()
    if settings.llm_provider == "bedrock":
        from app.agent.bedrock_client import converse_stream

        async for piece in converse_stream(
            prompt, system=system, max_tokens=num_predict, timeout_s=timeout_s
        ):
            yield piece
        return
    payload: dict[str, Any] = {
        "model": settings.ollama_model,
        "prompt": prompt,
        "stream": True,
        "keep_alive": "10m",
        "options": {"temperature": 0.3, "num_predict": num_predict},
    }
    if system:
        payload["system"] = system
    timeout = httpx.Timeout(timeout_s, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", f"{settings.ollama_base_url}/api/generate", json=payload) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                except ValueError:
                    continue
                piece = chunk.get("response")
                if piece:
                    yield piece
                if chunk.get("done"):
                    break
