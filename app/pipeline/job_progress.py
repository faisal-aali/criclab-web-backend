"""Progress-bar bands used by job ETA. Workers in criclab-video-service write these stages."""

from __future__ import annotations

ACTION_BANDS: dict[str, tuple[int, int]] = {
    "queued": (0, 0),
    "claimed": (0, 0),
    "ingest": (1, 8),
    "extract": (8, 12),
    "pose": (12, 48),
    "action": (48, 54),
    "ball": (54, 66),
    "metrics": (66, 70),
    "render": (70, 84),
    "upload": (84, 88),
    "agent": (88, 94),
    "pdf": (94, 99),
    "done": (100, 100),
}

BALLTRACK_BANDS: dict[str, tuple[int, int]] = {
    "queued": (0, 0),
    "claimed": (0, 0),
    "ingest": (1, 8),
    "calibrate": (8, 14),
    "detect": (14, 42),
    "track": (42, 58),
    "metrics": (58, 78),
    "render": (78, 90),
    "agent": (90, 99),
    "done": (100, 100),
}

ACTION_STAGE_ORDER = [
    "ingest",
    "extract",
    "pose",
    "action",
    "ball",
    "metrics",
    "render",
    "upload",
    "agent",
    "pdf",
]
ACTION_TIME_SHARE: dict[str, float] = {
    "ingest": 0.08,
    "extract": 0.02,
    "pose": 0.48,
    "action": 0.03,
    "ball": 0.12,
    "metrics": 0.02,
    "render": 0.15,
    "upload": 0.04,
    "agent": 0.04,
    "pdf": 0.02,
}

BALLTRACK_STAGE_ORDER = [
    "ingest",
    "calibrate",
    "detect",
    "track",
    "metrics",
    "render",
    "agent",
]
BALLTRACK_TIME_SHARE: dict[str, float] = {
    "ingest": 0.08,
    "calibrate": 0.04,
    "detect": 0.42,
    "track": 0.12,
    "metrics": 0.08,
    "render": 0.16,
    "agent": 0.10,
}


def work_done_fraction(
    *,
    stage: str | None,
    progress: int,
    bands: dict[str, tuple[int, int]],
    shares: dict[str, float],
    order: list[str],
) -> float:
    """How much of the clock this job has burned, 0–1."""
    progress = max(0, min(100, int(progress or 0)))
    if progress >= 100 or stage in ("done", "completed"):
        return 1.0
    done = 0.0
    for name in order:
        share = float(shares.get(name) or 0.0)
        if name == stage:
            lo, hi = bands.get(name, (0, 0))
            span = max(1, int(hi) - int(lo))
            frac = (progress - int(lo)) / span
            return min(1.0, max(0.0, done + share * min(1.0, max(0.0, frac))))
        done += share
    return min(1.0, progress / 100.0)
