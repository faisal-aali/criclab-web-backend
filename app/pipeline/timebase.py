"""Capture-rate recovery — the timebase every speed and duration rests on.

A phone slow-motion clip exported to a 30 fps container reports 30 fps in its
metadata while each frame really holds 1/120 s or 1/240 s of real time. Nothing
in the file says so. Every km/h and every millisecond derived from the container
rate is then wrong by the slow-down factor, silently and confidently.

Gravity is the ruler that catches it. A ball in free flight accelerates downward
at g = 9.81 m/s²; in image pixels that is

    a_px = g / (mpp · fps²)      pixels per frame²

so a parabola fitted to the tracked in-air path, plus the pixel scale we already
have from the bowler's height, solves for fps directly. Nothing here is a prior
about how fast people bowl — it is the same measurement a physicist would make.

We only override the container rate when the fit is strong (a statistically
significant quadratic term over enough of the flight) and the two disagree by
far more than fit noise could explain. Otherwise the container rate stands and
we say why.
"""

from __future__ import annotations

from typing import Any

import numpy as np

G_MPS2 = 9.81

# Rates real cameras actually shoot at. A measured value inside SNAP_TOL of one
# of these is reported as that rate — the fit is good to ~10%, not to 1 fps.
COMMON_RATES = (30.0, 48.0, 50.0, 60.0, 90.0, 100.0, 120.0, 200.0, 240.0)
SNAP_TOL = 0.20

MIN_FPS = 20.0
MAX_FPS = 600.0
# Below this the two rates agree inside what the fit can resolve — leave it alone.
DISAGREE_RATIO = 1.5
MIN_POINTS = 8
MIN_SPAN_FRAMES = 8
MIN_SIGNIFICANCE = 3.0  # sigma on the quadratic coefficient


def measure_capture_fps(
    ball_track: list[dict[str, Any]] | None,
    meters_per_pixel: float | None,
    container_fps: float,
) -> dict[str, Any]:
    """Solve the in-air path for the rate the clip was actually captured at.

    Returns a report that always carries `fps` (the rate to use downstream),
    `source`, and `note`; `slow_motion` is True only when we measured a rate
    materially faster than the container claims.
    """
    out: dict[str, Any] = {
        "fps": float(container_fps),
        "container_fps": float(container_fps),
        "measured_fps": None,
        "raw_measured_fps": None,
        "slow_motion": False,
        "slow_factor": None,
        "source": "container_metadata",
        "note": "Frame rate taken from the video file",
        "gravity_px_per_frame2": None,
        "significance": None,
        "points_used": 0,
    }
    if not meters_per_pixel or meters_per_pixel <= 0 or container_fps <= 0:
        out["note"] = "No pixel scale — cannot check the clip's frame rate against gravity"
        return out

    pts = _flight_points(ball_track)
    if len(pts) < MIN_POINTS:
        out["note"] = "Frame rate from the video file — no tracked ball flight to check it against"
        return out

    ts = np.array([float(p["frame"]) for p in pts])
    ys = np.array([float(p["y"]) for p in pts])
    if ts[-1] - ts[0] < MIN_SPAN_FRAMES:
        out["note"] = "Frame rate from the video file — tracked flight too short to check"
        return out

    fit = _quadratic_with_error(ts, ys)
    if fit is None:
        out["note"] = "Frame rate from the video file — could not fit the ball's flight"
        return out
    a_px, a_err = fit  # a_px = d²y/dframe² (positive = falling, image y grows down)
    out["gravity_px_per_frame2"] = round(a_px, 5)
    out["points_used"] = len(pts)

    if a_px <= 0:
        out["note"] = "Frame rate from the video file — tracked path does not curve like free flight"
        return out
    sig = a_px / a_err if a_err > 0 else 0.0
    out["significance"] = round(float(sig), 2)
    if sig < MIN_SIGNIFICANCE:
        out["note"] = (
            "Frame rate from the video file — the flight's curvature is too weak to "
            "measure the capture rate from gravity"
        )
        return out

    measured = float(np.sqrt(G_MPS2 / (float(meters_per_pixel) * a_px)))
    if not np.isfinite(measured) or not (MIN_FPS <= measured <= MAX_FPS):
        out["note"] = "Frame rate from the video file — gravity check gave an impossible rate"
        return out
    out["raw_measured_fps"] = round(measured, 1)

    ratio = measured / float(container_fps)
    if ratio < DISAGREE_RATIO:
        out["note"] = (
            f"Frame rate {container_fps:.0f} fps from the video file, confirmed against "
            f"the ball's fall ({measured:.0f} fps measured)"
        )
        return out

    used = _snap(measured)
    out.update(
        fps=used,
        measured_fps=used,
        slow_motion=True,
        slow_factor=round(used / float(container_fps), 2),
        source="ball_gravity",
        note=(
            f"Slow-motion clip: the file says {container_fps:.0f} fps but the ball falls at "
            f"{G_MPS2:.2f} m/s² only if it was captured at ~{used:.0f} fps "
            f"({used / float(container_fps):.1f}× slow motion). Speeds and timings use the "
            "measured rate — at the file's rate they would read that many times too slow."
        ),
    )
    return out


def _flight_points(ball_track: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Detected (never interpolated) in-air samples, time-ordered.

    Interpolated points come from the very parabola we are about to fit, so
    including them would manufacture the agreement we are testing for.
    """
    if not ball_track:
        return []
    pts = [p for p in ball_track if p.get("source") != "interpolated"]
    return sorted(pts, key=lambda p: int(p["frame"]))


def _quadratic_with_error(ts: np.ndarray, ys: np.ndarray) -> tuple[float, float] | None:
    """Return (d²y/dt², 1-sigma error on it) for y(t) = y0 + v·t + ½a·t²."""
    # Centre time to keep the Vandermonde matrix well-conditioned.
    t = ts - float(ts.mean())
    try:
        coeffs, cov = np.polyfit(t, ys, 2, cov=True)
    except Exception:
        return None
    if not np.all(np.isfinite(cov)):
        return None
    a_px = 2.0 * float(coeffs[0])
    a_err = 2.0 * float(np.sqrt(max(cov[0, 0], 0.0)))
    if not np.isfinite(a_px) or not np.isfinite(a_err):
        return None
    return a_px, a_err


def _snap(measured: float) -> float:
    """Snap to the nearest rate a camera actually offers, when it is within tolerance."""
    best = min(COMMON_RATES, key=lambda r: abs(measured - r) / r)
    return best if abs(measured - best) / best <= SNAP_TOL else round(measured, 1)
