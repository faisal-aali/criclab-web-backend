"""Camera-view quality — used to refuse speeds that would be false.

A single camera cannot see motion toward/away from the lens. Front-on and
steep three-quarter footage therefore cannot yield a truthful km/h. We
classify the view from pose (apparent shoulder width vs stature) and from
how much the bowling wrist travels sideways in the image.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from app.pipeline import pose as posemod
from app.pipeline.action import frame_by_index

# Apparent shoulder breadth / nose–ankle height.
# True side-on stacks the shoulders (~0.06–0.13). Front-on shows full breadth (~0.23+).
# Open-chested bowling actions sit ~0.18–0.22 and still show the arm wheel in plane.
FRONT_ON_RATIO = 0.23
THREE_QUARTER_RATIO = 0.145


def classify_camera_view(
    pose_track: dict[str, Any],
    action: dict[str, Any],
) -> dict[str, Any]:
    """Return view class and whether image-plane km/h would be a false number."""
    frames = pose_track.get("frames") or []
    fps = float(pose_track.get("fps") or 30.0)
    side = action.get("throwing_side")
    release = action.get("release_frame")
    result: dict[str, Any] = {
        "view": "unknown",
        "shoulder_width_ratio": None,
        "wrist_lateral_ratio": None,
        "speed_ok": False,
        "stride_ok": False,
        "note": "Could not classify camera angle from pose",
    }
    if not frames or side is None or release is None:
        return result

    ratios: list[float] = []
    span = max(2, int(round(fps * 0.12)))
    for f in frames:
        if abs(int(f["frame"]) - int(release)) > span:
            continue
        lsh = posemod.point(f, "left_shoulder")
        rsh = posemod.point(f, "right_shoulder")
        bh = posemod.body_pixel_height(f)
        if lsh is None or rsh is None or not bh:
            continue
        ratios.append(float(np.hypot(lsh[0] - rsh[0], lsh[1] - rsh[1])) / float(bh))
    shoulder_ratio = float(np.median(ratios)) if ratios else None

    wrist_lateral = _wrist_lateral_ratio(frames, side, int(release), fps)

    if shoulder_ratio is None:
        result["wrist_lateral_ratio"] = wrist_lateral
        if wrist_lateral is not None and wrist_lateral < 0.28:
            result.update(
                view="front_on",
                speed_ok=False,
                stride_ok=False,
                note="Bowling arm moves mostly toward the camera — km/h from this clip would be false",
            )
            return result
        result["note"] = "Shoulders not visible enough to classify camera angle"
        return result

    result["shoulder_width_ratio"] = round(shoulder_ratio, 3)
    result["wrist_lateral_ratio"] = None if wrist_lateral is None else round(wrist_lateral, 3)

    frontish_wrist = wrist_lateral is not None and wrist_lateral < 0.30

    if shoulder_ratio >= FRONT_ON_RATIO or (shoulder_ratio >= 0.17 and frontish_wrist):
        result.update(
            view="front_on",
            speed_ok=False,
            stride_ok=False,
            note="Front-on camera — image-plane km/h and stride would be false. Film side-on.",
        )
        return result

    if shoulder_ratio >= THREE_QUARTER_RATIO:
        # Three-quarter: depth is missing. Allow speed only if the arm clearly
        # travels across the frame (true-ish side component).
        if frontish_wrist or wrist_lateral is None:
            result.update(
                view="three_quarter",
                speed_ok=False,
                stride_ok=False,
                note="Three-quarter view — too much motion is toward the camera. Film side-on for speed.",
            )
            return result
        result.update(
            view="three_quarter",
            speed_ok=True,
            stride_ok=False,
            note="Three-quarter view — stride is foreshortened; speed is an image-plane estimate",
        )
        return result

    result.update(
        view="side_on",
        speed_ok=True,
        stride_ok=True,
        note="Side-on view — image-plane speed is the best this camera can do (not a radar gun)",
    )
    return result


def _wrist_lateral_ratio(
    frames: list[dict[str, Any]],
    side: str,
    release_frame: int,
    fps: float,
) -> float | None:
    """|Δx| / (|Δx|+|Δy|) of the bowling wrist in the 150 ms before release."""
    pre = max(3, int(round(fps * 0.15)))
    start = frame_by_index(frames, release_frame - pre)
    end = frame_by_index(frames, release_frame)
    if start is None or end is None:
        return None
    w0 = posemod.point(start, f"{side}_wrist")
    w1 = posemod.point(end, f"{side}_wrist")
    if w0 is None or w1 is None:
        return None
    dx = abs(float(w1[0] - w0[0]))
    dy = abs(float(w1[1] - w0[1]))
    den = dx + dy
    if den < 8:
        return None
    return dx / den


def flight_geometry_ok(
    ball_track: list[dict[str, Any]] | None,
    frame_w: int,
    frame_h: int,
) -> tuple[bool, str]:
    """Reject tracks that did not travel across the frame (depth-only / blob hop)."""
    if not ball_track or len(ball_track) < 6:
        return False, "Ball path too short to measure speed"
    pts = sorted(ball_track, key=lambda p: int(p["frame"]))
    net_x = abs(float(pts[-1]["x"]) - float(pts[0]["x"]))
    net_y = abs(float(pts[-1]["y"]) - float(pts[0]["y"]))
    net = float(np.hypot(net_x, net_y))
    min_net = max(70.0, 0.06 * float(frame_w or 1280))
    if net < min_net:
        return False, "Ball did not travel far enough in the image to measure speed"
    # Mostly vertical / toward camera: reporting km/h would under-read badly.
    if net_x < max(48.0, 0.045 * float(frame_w or 1280)) and net_x < net_y * 0.55:
        return False, "Ball motion is mostly toward the camera — a km/h figure would be false"
    # Stationary-ish blob (tree/post) that barely moves.
    span = max(1, int(pts[-1]["frame"]) - int(pts[0]["frame"]))
    if (net / span) < 2.5:
        return False, "Tracked object is too slow in the image to be a cricket ball"
    _ = frame_h
    return True, ""
