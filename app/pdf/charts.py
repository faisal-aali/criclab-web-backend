"""Matplotlib chart generation for the PDF report (headless / Agg backend)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402

PITCH = "#0B3D2E"
SEAM = "#C45C26"
BALL = "#B91C1C"
GRID = "#CBD5E0"


def _series(angle_series: list[dict[str, Any]], key: str) -> tuple[list[float], list[float]]:
    xs, ys = [], []
    for a in angle_series:
        if a.get(key) is not None and a.get("t_ms") is not None:
            xs.append(float(a["t_ms"]))
            ys.append(float(a[key]))
    return xs, ys


def _line_chart(out: Path, title: str, angle_series, specs: list[tuple[str, str, str]]) -> Path | None:
    fig, ax = plt.subplots(figsize=(7.2, 3.0), dpi=150)
    plotted = False
    for key, label, color in specs:
        xs, ys = _series(angle_series, key)
        if len(xs) >= 2:
            ax.plot(xs, ys, label=label, color=color, linewidth=2.2)
            plotted = True
    if not plotted:
        plt.close(fig)
        return None
    ax.set_title(title, color=PITCH, fontsize=12, fontweight="bold", loc="left")
    ax.set_xlabel("Time from front-foot contact (ms)", fontsize=9)
    ax.set_ylabel("Angle (deg)", fontsize=9)
    ax.grid(True, color=GRID, linewidth=0.6, alpha=0.7)
    ax.legend(loc="best", fontsize=8, frameon=False)
    for s in ax.spines.values():
        s.set_color(GRID)
    fig.tight_layout()
    fig.savefig(out, transparent=False)
    plt.close(fig)
    return out


def arm_angles_chart(out_dir: Path, angle_series) -> Path | None:
    return _line_chart(
        out_dir / "chart_arm_angles.png",
        "Bowling arm — joint angles through the delivery",
        angle_series,
        [("elbow_extension", "Elbow extension", SEAM),
         ("shoulder_abduction", "Shoulder abduction", PITCH)],
    )


def leg_trunk_chart(out_dir: Path, angle_series) -> Path | None:
    return _line_chart(
        out_dir / "chart_leg_trunk.png",
        "Trunk & legs — joint angles through the delivery",
        angle_series,
        [("trunk_flexion", "Trunk flexion", PITCH),
         ("front_knee_flexion", "Front-knee flexion", SEAM)],
    )


def wrist_speed_chart(out_dir: Path, wrist_speed_series, release_frame: int | None) -> Path | None:
    xs = [float(p["frame"]) for p in (wrist_speed_series or [])]
    ys = [float(p["speed_px"]) for p in (wrist_speed_series or [])]
    if len(xs) < 2:
        return None
    fig, ax = plt.subplots(figsize=(7.2, 3.0), dpi=150)
    ax.plot(xs, ys, color=BALL, linewidth=2.2, label="Bowling-hand speed (px/frame)")
    ax.fill_between(xs, ys, color=BALL, alpha=0.12)
    if release_frame is not None:
        ax.axvline(release_frame, color=PITCH, linestyle="--", linewidth=1.6)
        ax.text(release_frame, max(ys) * 0.96, " release", color=PITCH, fontsize=9, va="top")
    ax.set_title("Bowling-hand speed profile", color=PITCH, fontsize=12, fontweight="bold", loc="left")
    ax.set_xlabel("Frame", fontsize=9)
    ax.set_ylabel("Speed (px/frame)", fontsize=9)
    ax.grid(True, color=GRID, linewidth=0.6, alpha=0.7)
    for s in ax.spines.values():
        s.set_color(GRID)
    fig.tight_layout()
    out = out_dir / "chart_wrist_speed.png"
    fig.savefig(out, transparent=False)
    plt.close(fig)
    return out


def _velocity_series(angle_series: list[dict[str, Any]], key: str) -> tuple[list[float], list[float]]:
    xs, ys = _series(angle_series, key)
    if len(xs) < 3:
        return [], []
    vxs, vys = [], []
    for i in range(1, len(xs)):
        dt_s = (xs[i] - xs[i - 1]) / 1000.0
        if dt_s <= 1e-6:
            continue
        vxs.append(xs[i])
        vys.append((ys[i] - ys[i - 1]) / dt_s)
    if len(vys) < 3:
        return vxs, vys
    sm = [vys[0]]
    for i in range(1, len(vys) - 1):
        sm.append((vys[i - 1] + vys[i] + vys[i + 1]) / 3.0)
    sm.append(vys[-1])
    return vxs, sm


def _velocity_chart(out: Path, title: str, angle_series, specs: list[tuple[str, str, str]]) -> Path | None:
    fig, ax = plt.subplots(figsize=(7.2, 3.0), dpi=150)
    plotted = False
    for key, label, color in specs:
        xs, ys = _velocity_series(angle_series, key)
        if len(xs) >= 2:
            ax.plot(xs, ys, label=label, color=color, linewidth=2.0)
            plotted = True
    if not plotted:
        plt.close(fig)
        return None
    ax.axhline(0, color=GRID, linewidth=0.8)
    ax.set_title(title, color=PITCH, fontsize=12, fontweight="bold", loc="left")
    ax.set_xlabel("Time from front-foot contact (ms)", fontsize=9)
    ax.set_ylabel("Angular velocity (deg/s)", fontsize=9)
    ax.grid(True, color=GRID, linewidth=0.6, alpha=0.7)
    ax.legend(loc="best", fontsize=8, frameon=False)
    for s in ax.spines.values():
        s.set_color(GRID)
    fig.tight_layout()
    fig.savefig(out, transparent=False)
    plt.close(fig)
    return out


def arm_velocity_chart(out_dir: Path, angle_series) -> Path | None:
    return _velocity_chart(
        out_dir / "chart_arm_velocity.png",
        "Bowling arm — angular velocity (image plane)",
        angle_series,
        [("elbow_extension", "Elbow extension", SEAM),
         ("shoulder_abduction", "Shoulder abduction", PITCH)],
    )


def leg_trunk_velocity_chart(out_dir: Path, angle_series) -> Path | None:
    return _velocity_chart(
        out_dir / "chart_leg_trunk_velocity.png",
        "Trunk & legs — angular velocity (image plane)",
        angle_series,
        [("trunk_flexion", "Trunk flexion", PITCH),
         ("front_knee_flexion", "Front-knee flexion", SEAM)],
    )


def angle_velocity_range(angle_series: list[dict[str, Any]], key: str, clamp: float = 4000.0) -> dict[str, float | None]:
    """5th–95th percentile of d(angle)/dt. Reject (don't clamp) if outside band."""
    _, ys = _velocity_series(angle_series, key)
    if len(ys) < 4:
        return {"min": None, "max": None}
    import numpy as np
    lo = float(np.percentile(ys, 5))
    hi = float(np.percentile(ys, 95))
    if abs(lo) > clamp or abs(hi) > clamp:
        return {"min": None, "max": None}
    return {"min": round(lo, 0), "max": round(hi, 0)}
