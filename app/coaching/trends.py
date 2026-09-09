"""Aggregate Action deliveries into per-user training statistics.

Pure arithmetic over stored `status === ok` metrics. No LLM, no CV.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

MIN_SAMPLE = 3

# Numeric metrics we want trends for. Each key is a top-level key in delivery["metrics"].
_METRIC_KEYS = [
    "ball_speed_kmh",
    "arm_speed_kmh",
    "release_height_m",
    "release_time_ms",
    "arm_swing_speed_deg_s",
    "release_angle_deg",
    "stride_length_pct_height",
    "elbow_extension_deg",
    "front_knee_flexion_deg",
    "hip_shoulder_separation_deg",
]

# Within metric fields, the value lives under these keys.
_VALUE_KEYS = ("value",)


def _metric_value(m: Any) -> float | None:
    """Extract a clean float from a metric cell only when status is ok."""
    if not isinstance(m, dict):
        return None
    if m.get("status") != "ok":
        return None
    for k in _VALUE_KEYS:
        v = m.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _score_value(scores: dict[str, Any], key: str) -> float | None:
    """Score dict values are plain floats, not status cells."""
    v = scores.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_iso(dt: datetime | Any) -> str | None:
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _trend(values: list[float], n: int = 3) -> dict[str, Any] | None:
    """Compare mean of the most recent n values with the mean of all earlier values."""
    if len(values) < 2:
        return None
    recent_n = min(n, max(1, len(values) // 2))
    recent = values[-recent_n:]
    prior = values[:-recent_n] or values[:1]
    recent_mean = sum(recent) / len(recent)
    prior_mean = sum(prior) / len(prior)
    change = recent_mean - prior_mean
    pct = (change / prior_mean * 100.0) if prior_mean else None
    return {
        "recent_mean": round(recent_mean, 2),
        "prior_mean": round(prior_mean, 2),
        "change": round(change, 2),
        "percent_change": round(pct, 1) if pct is not None else None,
        "direction": "up" if change > 0 else ("down" if change < 0 else "flat"),
        "window": recent_n,
    }


def _best_for(key: str, values: list[float]) -> float | None:
    """Best value: max for speeds/heights/scores; min for release time."""
    if not values:
        return None
    if key == "release_time_ms":
        return min(values)
    return max(values)


def _rank_focus_areas(weakness_counts: dict[str, int]) -> list[dict[str, Any]]:
    """Rank tags by frequency; stable tie-break by tag name."""
    ranked = sorted(weakness_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"tag": tag, "count": count} for tag, count in ranked]


def build_training_profile(deliveries: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a deterministic training profile from a user's past Action deliveries.

    `deliveries` must be ordered newest-first (repo default). All timestamps are
    returned as ISO 8601 strings. No metric is synthesized; missing or non-ok
    values are omitted from series and aggregates.
    """
    # Work oldest -> newest for trend calculations and consistent display order.
    def _sort_key(d: dict[str, Any]) -> datetime:
        dt = d.get("created_at")
        if isinstance(dt, datetime):
            return dt
        return datetime.min.replace(tzinfo=timezone.utc)

    items = sorted(deliveries, key=_sort_key)

    player_profile: dict[str, Any] = {}
    created_dates: list[str] = []
    sample_size = 0

    for d in items:
        dt = _to_iso(d.get("created_at"))
        if dt:
            created_dates.append(dt)
            sample_size += 1

    # Use the most recent non-empty player profile (items are oldest -> newest).
    for d in reversed(items):
        pp = d.get("player_profile") or {}
        if isinstance(pp, dict) and pp:
            player_profile = pp
            break

    # Metric series and aggregates.
    metrics_out: dict[str, Any] = {}
    for key in _METRIC_KEYS:
        series: list[dict[str, Any]] = []
        values: list[float] = []
        for d in items:
            dt = _to_iso(d.get("created_at"))
            v = _metric_value((d.get("metrics") or {}).get(key))
            if v is not None and dt is not None:
                series.append({"date": dt, "value": round(v, 2)})
                values.append(v)

        ok = len(values) >= MIN_SAMPLE
        metrics_out[key] = {
            "series": series,
            "status": "ok" if ok else "insufficient_data",
            "count": len(values),
            "latest": round(values[-1], 2) if values else None,
            "best": round(_best_for(key, values), 2) if values else None,
            "mean": round(_mean(values), 2) if values else None,
            "trend": _trend(values) if ok else None,
        }

    # Score aggregates (plain floats under metrics["scores"]).
    scores_out: dict[str, Any] = {}
    score_keys = {"overall", "arm_speed", "ball_speed", "sequencing", "front_leg_brace", "hip_shoulder_separation"}
    for key in sorted(score_keys):
        series_s: list[dict[str, Any]] = []
        values_s: list[float] = []
        for d in items:
            dt = _to_iso(d.get("created_at"))
            v = _score_value((d.get("metrics") or {}).get("scores") or {}, key)
            if v is not None and dt is not None:
                series_s.append({"date": dt, "value": round(v, 1)})
                values_s.append(v)
        ok_s = len(values_s) >= MIN_SAMPLE
        scores_out[key] = {
            "series": series_s,
            "status": "ok" if ok_s else "insufficient_data",
            "count": len(values_s),
            "latest": round(values_s[-1], 1) if values_s else None,
            "best": round(max(values_s), 1) if values_s else None,
            "mean": round(_mean(values_s), 1) if values_s else None,
            "trend": _trend(values_s) if ok_s else None,
        }

    # Recurring focus areas from persisted weakness tags.
    weakness_counts: dict[str, int] = {}
    tagged = 0
    for d in items:
        tags = (d.get("analysis") or {}).get("weakness_tags") or []
        if tags:
            tagged += 1
            for t in tags:
                weakness_counts[t] = weakness_counts.get(t, 0) + 1

    focus_areas = _rank_focus_areas(weakness_counts)
    for fa in focus_areas:
        fa["coverage_count"] = tagged

    overall = scores_out.get("overall", {})
    overall_score = overall.get("latest")

    return {
        "player_profile": player_profile,
        "sample_size": sample_size,
        "date_range": {"first": created_dates[0] if created_dates else None, "latest": created_dates[-1] if created_dates else None},
        "min_sample_required": MIN_SAMPLE,
        "overall_score": round(overall_score, 1) if overall_score is not None else None,
        "metrics": metrics_out,
        "scores": scores_out,
        "focus_areas": focus_areas,
        "focus_areas_status": "ok" if tagged >= MIN_SAMPLE else "insufficient_data",
    }
