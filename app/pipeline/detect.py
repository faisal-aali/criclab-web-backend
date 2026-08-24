"""Foreground-based player/ball detection for MVP.

Strategy: MOG2 background subtraction isolates moving foreground. The player is
the largest tall blob. Ball candidates are small, roughly-circular fast blobs.
We return MULTIPLE ball candidates per frame and let the tracker pick the one
consistent trajectory (this is what removes grass / cloud / camera-shake noise).
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from app.pipeline.cv_vision import enhance_bgr


def make_background_subtractor() -> Any:
    return cv2.createBackgroundSubtractorMOG2(history=200, varThreshold=32, detectShadows=False)


def foreground_mask(bg, frame: np.ndarray) -> np.ndarray:
    mask = bg.apply(frame)
    _, mask = cv2.threshold(mask, 200, 255, cv2.THRESH_BINARY)
    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
    mask = cv2.dilate(mask, kernel, iterations=1)
    return mask


def detect_player_bbox(mask: np.ndarray, frame_shape: tuple[int, int]) -> dict[str, Any] | None:
    """Largest reasonably-tall foreground blob = bowler proxy (for scale)."""
    h, w = frame_shape
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    best_area = 0.0
    for c in contours:
        area = cv2.contourArea(c)
        if area < (w * h) * 0.004:
            continue
        x, y, bw, bh = cv2.boundingRect(c)
        if bh < bw * 0.9:  # want upright person-like shape
            continue
        if area > best_area:
            best_area = area
            best = {"x": x, "y": y, "w": bw, "h": bh, "cx": x + bw / 2, "cy": y + bh / 2}
    return best


def detect_ball_candidates(
    mask: np.ndarray,
    frame_shape: tuple[int, int],
    player: dict[str, Any] | None,
    max_candidates: int = 12,
) -> list[dict[str, Any]]:
    """Return small round foreground blobs as ball hypotheses (unranked-ish)."""
    h, w = frame_shape
    min_r = max(2.0, min(w, h) * 0.004)
    max_r = min(w, h) * 0.045
    min_area = np.pi * min_r * min_r * 0.4
    max_area = np.pi * max_r * max_r * 2.5

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cands: list[dict[str, Any]] = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < min_area or area > max_area:
            continue
        (cx, cy), radius = cv2.minEnclosingCircle(c)
        if radius < min_r or radius > max_r:
            continue
        circularity = area / (np.pi * radius * radius + 1e-6)
        if circularity < 0.45:
            continue
        # Prefer compact blobs away from the player's large torso mass
        score = circularity
        if player is not None:
            inside_x = player["x"] - 4 <= cx <= player["x"] + player["w"] + 4
            inside_y = player["y"] - 4 <= cy <= player["y"] + player["h"] + 4
            if inside_x and inside_y:
                score *= 0.6  # de-prioritize but keep (release happens near body)
        cands.append(
            {"x": float(cx), "y": float(cy), "r": float(radius), "score": float(score)}
        )

    cands.sort(key=lambda d: d["score"], reverse=True)
    return cands[:max_candidates]


def _blob_candidates(
    mask: np.ndarray,
    origin_xy: tuple[int, int] = (0, 0),
    max_candidates: int = 24,
    *,
    max_r_frac: float = 0.08,
) -> list[dict[str, Any]]:
    h, w = mask.shape[:2]
    min_r = max(2.0, min(w, h) * 0.002)
    max_r = min(w, h) * float(max_r_frac)
    cricket_r = min(w, h) * 0.028  # white/red cricket ball; training ovals can be larger
    ox, oy = origin_xy
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cands: list[dict[str, Any]] = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < 8:
            continue
        (cx, cy), radius = cv2.minEnclosingCircle(c)
        if radius < min_r or radius > max_r:
            continue
        circularity = area / (np.pi * radius * radius + 1e-6)
        # Motion-blurred cricket/football is oval — keep elongated blobs.
        if circularity < 0.12:
            continue
        # Size-favoring score keeps large training ovals; compact score keeps a
        # 4–16 px cricket ball that posters and floodlight flares would drown.
        size_score = float(radius * (0.25 + circularity))
        compact_score = float(circularity * (1.2 + cricket_r / max(radius, 2.0)))
        cands.append({
            "x": float(cx + ox),
            "y": float(cy + oy),
            "r": float(radius),
            "score": max(size_score, compact_score),
            "size_score": size_score,
            "compact_score": compact_score,
        })
    if not cands:
        return []
    keep: dict[tuple[int, int], dict[str, Any]] = {}
    by_size = sorted(cands, key=lambda d: d["size_score"], reverse=True)
    by_compact = sorted(
        [c for c in cands if c["r"] <= cricket_r * 1.35],
        key=lambda d: d["compact_score"],
        reverse=True,
    )
    for c in by_size[: max(8, max_candidates // 2)]:
        keep[(int(c["x"]), int(c["y"]))] = c
    for c in by_compact[: max(8, max_candidates // 2)]:
        keep[(int(c["x"]), int(c["y"]))] = c
    return list(keep.values())[: max_candidates]


def detect_dark_flight_candidates(bgr: np.ndarray, *, mask_ground: bool = True) -> list[dict[str, Any]]:
    """Dark ball against sky/background (red cricket ball or dark football)."""
    bgr = enhance_bgr(bgr)
    h, w = bgr.shape[:2]
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    dark = ((hsv[:, :, 2] < 125) & (gray < 135)).astype(np.uint8) * 255
    # Ignore the turf on full frames — the ball we care about is in flight.
    if mask_ground and h >= 400:
        dark[int(h * 0.62) :, :] = 0
    kernel = np.ones((3, 3), np.uint8)
    dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, kernel, iterations=1)
    return _blob_candidates(dark, max_candidates=24)


def detect_ball_color_candidates(bgr: np.ndarray) -> list[dict[str, Any]]:
    """Red or white cricket-ball coloured blobs.

    White leather is often V~150–180 (not a clipped 185 highlight), and floodlight
    flares / posters are huge — cap radius at cricket-ball scale so they cannot
    crowd the candidate list. Dark training ovals still come from the dark detector.
    """
    bgr = enhance_bgr(bgr)
    h, w = bgr.shape[:2]
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    red = cv2.bitwise_or(
        cv2.inRange(hsv, (0, 70, 50), (12, 255, 255)),
        cv2.inRange(hsv, (168, 70, 50), (180, 255, 255)),
    )
    white = cv2.inRange(hsv, (0, 0, 150), (180, 85, 255))
    mask = cv2.bitwise_or(red, white)
    # Floodlight streaks live in the top of night clips.
    if h >= 400:
        mask[: int(h * 0.08), :] = 0
    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
    return _blob_candidates(mask, max_r_frac=0.038)


def detect_bright_flight_candidates(bgr: np.ndarray, *, mask_edges: bool = True) -> list[dict[str, Any]]:
    """White cricket ball as a small local brightness peak (night turf / grey wall).

    HSV-white alone matches shoes, stumps and poster lettering. A ball in flight
    is also brighter than its immediate surround — DoG keeps those discs and
    drops extended glare.
    """
    bgr = enhance_bgr(bgr)
    h, w = bgr.shape[:2]
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    white = cv2.inRange(hsv, (0, 0, 145), (180, 90, 255))
    surround = cv2.GaussianBlur(gray, (15, 15), 0)
    peak = cv2.subtract(gray, surround)
    _, peak_m = cv2.threshold(peak, 10, 255, cv2.THRESH_BINARY)
    mask = cv2.bitwise_and(white, peak_m)
    if mask_edges and h >= 400:
        mask[: int(h * 0.08), :] = 0
        mask[int(h * 0.78) :, :] = 0  # turf / planted white ball from a previous delivery
    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
    return _blob_candidates(mask, max_candidates=16, max_r_frac=0.032)


def detect_ball_motion_candidates(prev_gray: np.ndarray, gray: np.ndarray) -> list[dict[str, Any]]:
    """Small moving blobs from frame-to-frame difference."""
    diff = cv2.absdiff(gray, prev_gray)
    _, mask = cv2.threshold(diff, 22, 255, cv2.THRESH_BINARY)
    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
    return _blob_candidates(mask)


def detect_ball_in_roi(
    bgr: np.ndarray,
    prev_gray: np.ndarray | None,
    cx: float,
    cy: float,
    radius: float,
) -> list[dict[str, Any]]:
    """Ball hypotheses in a window around the predicted position."""
    h, w = bgr.shape[:2]
    r = int(max(24, radius))
    x0 = max(0, int(cx) - r)
    y0 = max(0, int(cy) - r)
    x1 = min(w, int(cx) + r)
    y1 = min(h, int(cy) + r)
    if x1 - x0 < 12 or y1 - y0 < 12:
        return []
    crop = bgr[y0:y1, x0:x1]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    color = detect_ball_color_candidates(crop)
    for c in color:
        c["x"] += x0
        c["y"] += y0
    dark = detect_dark_flight_candidates(crop, mask_ground=False)
    for c in dark:
        c["x"] += x0
        c["y"] += y0
    bright = detect_bright_flight_candidates(crop, mask_edges=False)
    for c in bright:
        c["x"] += x0
        c["y"] += y0
    motion: list[dict[str, Any]] = []
    if prev_gray is not None:
        prev_crop = prev_gray[y0:y1, x0:x1]
        if prev_crop.shape[:2] == gray.shape[:2]:
            motion = detect_ball_motion_candidates(prev_crop, gray)
            for c in motion:
                c["x"] += x0
                c["y"] += y0
    return merge_ball_candidates(color, dark, bright, motion)


def merge_ball_candidates(
    *groups: list[dict[str, Any]],
    merge_px: float = 8.0,
    per_group: int = 12,
    total: int = 40,
) -> list[dict[str, Any]]:
    """Spatial merge with a per-source quota.

    Colour posters score higher than a 6 px white ball (radius-weighted). Taking
    the global top-N therefore dropped the ball. Each detector keeps its own
    best hits, then we collapse duplicates.
    """
    merged: list[dict[str, Any]] = []
    for group in groups:
        ranked = sorted(group, key=lambda d: d.get("score") or 0, reverse=True)[:per_group]
        for c in ranked:
            hit = None
            for m in merged:
                if np.hypot(c["x"] - m["x"], c["y"] - m["y"]) <= merge_px:
                    hit = m
                    break
            if hit is None:
                merged.append(dict(c))
            else:
                hit["score"] = max(float(hit.get("score") or 0), float(c.get("score") or 0))
    merged.sort(key=lambda d: d.get("score") or 0, reverse=True)
    return merged[:total]
