"""Ollama agent — reasons over structured metrics only."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.config import get_settings


_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clean(text: str) -> str:
    return _CONTROL_RE.sub("", text or "").strip()


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


async def generate_text(prompt: str, system: str | None = None, *, num_predict: int = 500) -> str:
    settings = get_settings()
    payload: dict[str, Any] = {
        "model": settings.ollama_model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": "10m",
        "options": {"temperature": 0.3, "num_predict": num_predict},
    }
    if system:
        payload["system"] = system
    timeout = httpx.Timeout(300.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(f"{settings.ollama_base_url}/api/generate", json=payload)
        r.raise_for_status()
        return (r.json().get("response") or "").strip()


def get_delivery_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """Compact, LLM-friendly view of metrics (no bulky trajectory arrays)."""

    def mv(key: str) -> dict[str, Any] | None:
        m = metrics.get(key)
        if not isinstance(m, dict):
            return None
        status = m.get("status")
        value = m.get("value") if status == "ok" else None
        return {
            "value": value,
            "unit": m.get("unit"),
            "confidence": m.get("confidence"),
            "estimated": m.get("estimated"),
            "status": status,
            "note": m.get("note"),
        }

    scale = metrics.get("scale") or {}
    quality = metrics.get("quality") or {}
    return {
        "throwing_side": metrics.get("throwing_side"),
        "release_frame": metrics.get("release_frame"),
        "ball_speed_kmh": mv("ball_speed_kmh"),
        "ball_speed_mps": mv("ball_speed_mps"),
        "arm_speed_kmh": mv("arm_speed_kmh"),
        "arm_speed_mps": mv("arm_speed_mps"),
        "hand_speed_px_per_frame": mv("hand_speed_px_per_frame"),
        "release_time_ms": mv("release_time_ms"),
        "arm_swing_speed_deg_s": mv("arm_swing_speed_deg_s"),
        "release_height_m": mv("release_height_m"),
        "release_angle_deg": mv("release_angle_deg"),
        "stride_length_pct_height": mv("stride_length_pct_height"),
        "elbow_extension_deg": mv("elbow_extension_deg"),
        "front_knee_flexion_deg": mv("front_knee_flexion_deg"),
        "hip_shoulder_separation_deg": mv("hip_shoulder_separation_deg"),
        "line_m": mv("line_m"),
        "length_m": mv("length_m"),
        "scores": metrics.get("scores"),
        "sequencing_ok": metrics.get("sequencing_ok"),
        "scale": {"method": scale.get("method"), "calibrated": scale.get("calibrated"), "note": scale.get("note")},
        "quality": {
            "pose_frames": quality.get("pose_frames"),
            "tracking_ok": quality.get("tracking_ok"),
            "calibrated": quality.get("calibrated"),
            "has_front_foot_contact": quality.get("has_front_foot_contact"),
            "camera_view": quality.get("camera_view"),
            "camera_view_note": quality.get("camera_view_note"),
            "speed_view_ok": quality.get("speed_view_ok"),
        },
    }


def compare_deliveries(current: dict[str, Any], previous: list[dict[str, Any]]) -> dict[str, Any]:
    def speed(m: dict[str, Any]) -> float | None:
        for key in ("ball_speed_kmh", "arm_speed_kmh"):
            cell = m.get(key) or {}
            if cell.get("status") != "ok":
                continue
            v = cell.get("value")
            if v is None:
                continue
            v = float(v)
            if v < 20.0 or v > 175.0:
                continue
            return v
        return None

    cur = speed(current)
    prev_speeds = [speed(p) for p in previous]
    prev_speeds = [s for s in prev_speeds if s is not None]
    delta = None
    if cur is not None and prev_speeds:
        delta = cur - (sum(prev_speeds) / len(prev_speeds))
    return {
        "current_speed_kmh": cur,
        "previous_avg_speed_kmh": (sum(prev_speeds) / len(prev_speeds)) if prev_speeds else None,
        "delta_kmh": delta,
        "previous_count": len(prev_speeds),
    }


async def generate_report(
    *,
    metrics: dict[str, Any],
    comparison: dict[str, Any] | None = None,
    player_name: str = "Bowler",
    player_profile: dict[str, Any] | None = None,
    allowed_drills: list[dict[str, Any]] | None = None,
    candidate_tags: list[str] | None = None,
) -> dict[str, Any]:
    """Tool: generateReport — coaching narrative + catalog-only drill IDs."""
    from app.coaching.recommend import (
        allowed_drill_summaries,
        hydrate_recommendations,
        weakness_tags,
    )

    profile = player_profile or metrics.get("player_profile") or {}
    tags = list(candidate_tags) if candidate_tags is not None else weakness_tags(metrics)
    drills = allowed_drills if allowed_drills is not None else allowed_drill_summaries(tags)
    tool_payload = {
        "player": {
            "name": profile.get("player_name") or player_name,
            "age_years": profile.get("age_years"),
            "height_m": profile.get("height_m"),
            "weight_lbs": profile.get("weight_lbs"),
            "bowling_arm": profile.get("bowling_arm"),
            "bowling_style": profile.get("bowling_style"),
        },
        "metrics": get_delivery_metrics(metrics),
        "comparison": comparison or {},
        "candidate_tags": tags,
        "allowed_drills": drills,
    }
    system = (
        "You are Cric-Lab, an AI cricket bowling coach. "
        "Measurements come from pose estimation or stump-calibrated ball flight. "
        "Use ONLY the provided JSON; never invent or fill in null values. "
        "You do not measure speed or angles — never invent km/h, metres, or degrees. "
        "Coach to this bowler's age, height, bowling arm, and style (pace/spin/medium). "
        "Never claim radar-grade ball speed unless ball_speed_kmh status is ok — that value is still an estimate. "
        "If ball speed is unavailable or camera_view is front_on, say so and do not substitute arm/hand speed as ball speed. "
        "Never claim true 3D hip/trunk rotation from one camera. "
        "Give cricket-specific coaching from the available angles and timing. "
        "Keep each prose section to 1-3 short sentences. "
        "After CONFIDENCE_NOTE, add RECOMMENDATIONS: a JSON array of objects "
        '{"drill_id","reason","priority"} using ONLY drill_id values from allowed_drills. '
        "Pick 2-3 drills that match candidate_tags. Do not invent ids or URLs. "
        "Respond in plain text with these labeled sections exactly:\n"
        "SUMMARY:\nOBSERVATIONS:\nSTRENGTHS:\nIMPROVEMENTS:\nCONFIDENCE_NOTE:\nRECOMMENDATIONS:"
    )
    prompt = (
        f"Player: {player_name}\n"
        f"Measured data JSON:\n{json.dumps(tool_payload)}\n\n"
        "Write the coaching report sections now."
    )

    try:
        text = _clean(await generate_text(prompt, system=system, num_predict=750))
    except Exception as e:
        text = _fallback_report(metrics, comparison, error=str(e), tags=tags)

    sections = _parse_sections(text)
    recs = hydrate_recommendations(_parse_recommendation_list(sections.get("RECOMMENDATIONS", "")), tags=tags)
    return {
        "raw": text,
        "summary": _clean(sections.get("SUMMARY", text[:500])),
        "observations": _clean(sections.get("OBSERVATIONS", "")),
        "strengths": _clean(sections.get("STRENGTHS", "")),
        "improvements": _clean(sections.get("IMPROVEMENTS", "")),
        "confidence_note": _clean(
            sections.get("CONFIDENCE_NOTE", "Metrics are video-derived estimates.")
        ),
        "recommendations": recs,
        "comparison": comparison or {},
    }


def _parse_sections(text: str) -> dict[str, str]:
    keys = ["SUMMARY", "OBSERVATIONS", "STRENGTHS", "IMPROVEMENTS", "CONFIDENCE_NOTE", "RECOMMENDATIONS"]
    out: dict[str, str] = {}
    current = None
    buf: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        matched = None
        for k in keys:
            if stripped.upper().startswith(k + ":") or stripped.upper() == k:
                matched = k
                break
        if matched:
            if current:
                out[current] = "\n".join(buf).strip()
            current = matched
            rest = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
            buf = [rest] if rest else []
        else:
            buf.append(line)
    if current:
        out[current] = "\n".join(buf).strip()
    return out


def _parse_recommendation_list(blob: str) -> list[dict[str, Any]]:
    text = (blob or "").strip()
    if not text:
        return []
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def _fallback_report(
    metrics: dict[str, Any],
    comparison: dict[str, Any] | None,
    error: str,
    tags: list[str] | None = None,
) -> str:
    from app.coaching.recommend import fallback_picks

    ball = metrics.get("ball_speed_kmh") or {}
    ball_ok = ball.get("status") == "ok" and ball.get("value") is not None
    speed = ball.get("value") if ball_ok else None
    angle_m = metrics.get("release_angle_deg") or {}
    angle = angle_m.get("value") if angle_m.get("status") == "ok" else None
    cal = (metrics.get("scale") or {}).get("calibrated")
    if speed is not None:
        speed_txt = f"{speed:.1f} km/h (image-plane estimate)"
    else:
        speed_txt = "unavailable"
    summary = (
        f"Delivery analyzed. Ball speed {speed_txt}; "
        f"release angle {f'{angle:.1f}°' if angle is not None else 'n/a'}."
    )
    conf = (
        "Uncalibrated estimate — validate with radar when possible."
        if not cal
        else "Calibrated scale used; still an estimate, not a speed gun."
    )
    hist = ""
    if comparison and comparison.get("delta_kmh") is not None:
        hist = f"Speed delta vs recent average: {comparison['delta_kmh']:+.1f} km/h."
    recs = fallback_picks(tags or [])
    rec_json = json.dumps(
        [{"drill_id": r["drill_id"], "reason": r["reason"], "priority": r["priority"]} for r in recs]
    )
    return (
        f"SUMMARY:\n{summary} {hist}\n\n"
        f"OBSERVATIONS:\nRelease frame={'detected' if metrics.get('release_frame') is not None else 'uncertain'}. "
        f"(LLM unavailable: {error})\n\n"
        "STRENGTHS:\nConsistent tracking window available for review.\n\n"
        "IMPROVEMENTS:\nUse a side-on camera for Action mechanics, or Ball flight (behind-bowler + both wickets) for ICC-style speed.\n\n"
        f"CONFIDENCE_NOTE:\n{conf}\n\n"
        f"RECOMMENDATIONS:\n{rec_json}"
    )
