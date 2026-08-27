"""Estimated time remaining for a running analysis job.

Blends two signals, because neither is trustworthy alone:

* **This job's own progress.** Early on, "5% done after 3 seconds" says almost
  nothing about the total — the queued/extract stage is quick relative to pose
  estimation, so a naive `elapsed / progress` extrapolation wildly overshoots
  at low progress and undershoots once the heavy stages are behind it.
* **How long recent jobs of the same kind actually took.** This is real
  processing information — video length, machine load, model warm state —
  captured from the last N completed jobs rather than guessed at.

The blend shifts from "trust the average" to "trust this job's own pace" as
progress advances, since a live job's own elapsed time becomes more informative
than a historical average the further into its own run it gets.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.db.mongo import get_db

# Used only until real completions exist for a pipeline, or if none completed
# recently — a cold cache should not show nothing.
DEFAULT_TOTAL_SECONDS: dict[str, float] = {
    "action": 95.0,
    "ballflight": 70.0,
}

HISTORY_SAMPLE = 20
MIN_PROGRESS_FOR_LIVE_SIGNAL = 4  # below this, elapsed/progress is too noisy to use at all
FLOOR_SECONDS = 2.0
CEILING_SECONDS = 20 * 60  # never claim more than 20 minutes remain; something is wrong past that


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _historical_average_seconds(collection: str, pipeline: str) -> float | None:
    """Mean wall-clock duration of the last `HISTORY_SAMPLE` completed jobs."""
    db = get_db()
    cursor = (
        db[collection]
        .find(
            {"status": "completed", "created_at": {"$exists": True}, "updated_at": {"$exists": True}},
            {"created_at": 1, "updated_at": 1},
        )
        .sort("updated_at", -1)
        .limit(HISTORY_SAMPLE)
    )
    durations: list[float] = []
    async for doc in cursor:
        start, end = _aware(doc.get("created_at")), _aware(doc.get("updated_at"))
        if not start or not end:
            continue
        seconds = (end - start).total_seconds()
        if 1.0 <= seconds <= CEILING_SECONDS:
            durations.append(seconds)
    if not durations:
        return None
    return sum(durations) / len(durations)


async def estimate_eta_seconds(
    *, collection: str, pipeline: str, job: dict[str, Any]
) -> int | None:
    """Seconds remaining for a processing job, or None while too little is known.

    `collection` is the Mongo collection the job lives in ("jobs" or
    "balltrack_jobs"); `pipeline` picks the default baseline when history is
    thin.
    """
    status = job.get("status")
    if status in ("completed", "failed"):
        return None

    progress = int(job.get("progress") or 0)
    created_at = _aware(job.get("created_at"))
    if not created_at or progress <= 0:
        return None

    now = datetime.now(timezone.utc)
    elapsed = (now - created_at).total_seconds()
    if elapsed <= 0:
        return None

    historical_total = await _historical_average_seconds(collection, pipeline)
    baseline_total = historical_total or DEFAULT_TOTAL_SECONDS.get(pipeline, 90.0)

    if progress < MIN_PROGRESS_FOR_LIVE_SIGNAL:
        # Too early in this specific run for its own pace to mean anything —
        # go entirely on the baseline, minus what has already elapsed.
        remaining = baseline_total - elapsed
    else:
        live_total_estimate = elapsed / (progress / 100.0)
        # Weight shifts toward the live estimate as the job gets further along:
        # at 4% it is almost all baseline, by ~60% it is almost all live.
        live_weight = min(1.0, progress / 60.0)
        blended_total = baseline_total * (1 - live_weight) + live_total_estimate * live_weight
        remaining = blended_total - elapsed

    remaining = max(FLOOR_SECONDS, min(remaining, CEILING_SECONDS))
    return int(round(remaining))
