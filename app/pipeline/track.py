"""Trajectory extraction from per-frame ball candidates.

Robust method: RANSAC motion-model fitting. A ball in flight follows
    x(t) ~ x0 + vx*t          (near-constant horizontal pixel velocity)
    y(t) ~ y0 + vy*t + a*t^2   (parabola under gravity, in image pixels)
where t is the frame index. Random noise from grass, clouds, and camera shake
does NOT fit one consistent model across many consecutive frames, so it is
rejected as outliers. This is far more reliable than greedy nearest-neighbour
chaining for messy handheld footage.
"""

from __future__ import annotations

import random
from typing import Any

import cv2
import numpy as np

from app.pipeline.cv_vision import hough_ball_candidates, validate_ball_path_on_video


def _dist(a: dict[str, Any], b: dict[str, Any]) -> float:
    return float(np.hypot(a["x"] - b["x"], a["y"] - b["y"]))


def build_trajectory(
    frames: list[dict[str, Any]],
    frame_w: int,
    frame_h: int,
    min_len: int = 5,
    iterations: int = 600,
    seed: int = 12345,
) -> list[dict[str, Any]]:
    """Return the inlier ball-flight points (time-ordered) of the best model."""
    rng = random.Random(seed)
    diag = float(np.hypot(frame_w, frame_h)) or 1.0
    tol = diag * 0.020  # inlier distance tolerance in pixels
    min_net = diag * 0.05  # require real displacement

    frames_with = [f for f in frames if f["candidates"]]
    if len(frames_with) < min_len:
        return []

    # Flatten candidates keyed by frame for fast inlier search.
    frame_index = [f["frame"] for f in frames_with]
    cand_by_frame = {f["frame"]: f["candidates"] for f in frames_with}

    best_inliers: list[dict[str, Any]] = []

    n = len(frames_with)
    for _ in range(iterations):
        # Sample 3 distinct frames within a short temporal window (a throw is brief).
        i0 = rng.randint(0, n - 3)
        window = frames_with[i0 : min(n, i0 + 30)]
        if len(window) < 3:
            continue
        picks = rng.sample(window, 3)
        picks.sort(key=lambda f: f["frame"])
        ts = [float(f["frame"]) for f in picks]
        if ts[0] == ts[2]:
            continue
        pxs = [rng.choice(f["candidates"]) for f in picks]
        xs = [p["x"] for p in pxs]
        ys = [p["y"] for p in pxs]

        try:
            cx = np.polyfit(ts, xs, 1)  # linear x(t)
            cy = np.polyfit(ts, ys, 2)  # quadratic y(t)
        except Exception:
            continue

        # Count inliers: best candidate per frame within tolerance.
        inliers: list[dict[str, Any]] = []
        for fr in frame_index:
            px = np.polyval(cx, fr)
            py = np.polyval(cy, fr)
            best_c = None
            best_d = tol
            for c in cand_by_frame[fr]:
                d = float(np.hypot(c["x"] - px, c["y"] - py))
                if d < best_d:
                    best_d = d
                    best_c = c
            if best_c is not None:
                inliers.append({"frame": int(fr), "x": float(best_c["x"]), "y": float(best_c["y"]), "r": float(best_c["r"])})

        if len(inliers) < min_len:
            continue
        inliers.sort(key=lambda p: p["frame"])
        net = _dist(inliers[0], inliers[-1])
        if net < min_net:
            continue
        if len(inliers) > len(best_inliers):
            best_inliers = inliers

    if not best_inliers:
        return []

    return _largest_contiguous(best_inliers)


def _largest_contiguous(points: list[dict[str, Any]], max_gap: int = 8) -> list[dict[str, Any]]:
    """Keep the longest run of points without large frame gaps (one flight)."""
    if not points:
        return points
    runs: list[list[dict[str, Any]]] = [[points[0]]]
    for prev, cur in zip(points, points[1:]):
        if cur["frame"] - prev["frame"] <= max_gap:
            runs[-1].append(cur)
        else:
            runs.append([cur])
    return max(runs, key=len)


def estimate_release(
    ball_track: list[dict[str, Any]],
    player_track: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Release ~ first point of the clean flight path (ball leaving the hand)."""
    if len(ball_track) < 2:
        return None
    start = ball_track[0]
    conf = min(0.8, 0.4 + 0.04 * len(ball_track))
    return {
        "frame": int(start["frame"]),
        "x": float(start["x"]),
        "y": float(start["y"]),
        "confidence": float(conf),
    }


def track_ball_from_release(
    video_path,
    *,
    release_frame: int,
    wrist_xy: tuple[float, float],
    fps: float,
    meters_per_pixel: float | None = None,
    frame_w: int = 1280,
    frame_h: int = 720,
    throw_dir: tuple[float, float] | None = None,
) -> list[dict[str, Any]]:
    """Track the ball in the air after release, one breakpoint per frame.

    Lock onto the object that *leaves* the hand and keeps moving downrange.
    A cricket ball is a small red/white disc; a training/football is a dark oval
    against the sky. We never treat the bowling hand itself as the ball.
    """
    from app.pipeline import detect, extract

    if release_frame is None or wrist_xy is None:
        return []

    start = max(0, int(release_frame) - 2)
    end = int(release_frame) + min(90, max(24, int(round(fps * 0.40))))
    hand_x, hand_y = float(wrist_xy[0]), float(wrist_xy[1])
    dx, dy = (1.0, -0.35)
    if throw_dir is not None:
        n = float(np.hypot(throw_dir[0], throw_dir[1])) or 1.0
        dx, dy = float(throw_dir[0]) / n, float(throw_dir[1]) / n

    per_frame: list[dict[str, Any]] = []
    prev_gray = None
    for idx, bgr in extract.iter_frame_range(video_path, start, end):
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        elapsed = max(0, idx - int(release_frame))
        dark = detect.detect_dark_flight_candidates(bgr)
        color = detect.detect_ball_color_candidates(bgr)
        motion = detect.detect_ball_motion_candidates(prev_gray, gray) if prev_gray is not None else []
        hough = hough_ball_candidates(bgr)
        cands = detect.merge_ball_candidates(dark, color, motion, hough)
        min_hand = 48.0 if elapsed >= 4 else 12.0
        max_r = min(frame_w, frame_h) * 0.065
        # Downrange side follows the throw direction — never assume rightward.
        rightward = dx >= 0
        edge_near, edge_far = (24.0, 48.0) if rightward else (48.0, 24.0)
        filtered = []
        for c in cands:
            if float(c.get("r") or 0) > max_r:
                continue
            if c["x"] < edge_near or c["x"] > frame_w - edge_far:
                continue
            # Large blobs clipped by the downrange edge are trees/posts, not the ball.
            at_downrange_edge = c["x"] > frame_w * 0.88 if rightward else c["x"] < frame_w * 0.12
            if at_downrange_edge and float(c.get("r") or 0) > 28:
                continue
            dh = float(np.hypot(c["x"] - hand_x, c["y"] - hand_y))
            if dh < min_hand:
                continue
            if c["y"] > frame_h * 0.72:
                continue
            down = (c["x"] - hand_x) * dx + (c["y"] - hand_y) * dy
            if elapsed >= 4 and down < 8:
                continue
            filtered.append(c)
        per_frame.append({"frame": int(idx), "candidates": filtered})
        prev_gray = gray

    path = _best_flight_path(per_frame, hand_x, hand_y, dx, dy, fps, meters_per_pixel, frame_w, frame_h)
    if len(path) < 6:
        return []
    path = _ballistic_clean(path, frame_w, frame_h)
    if len(path) < 6:
        return []
    path = _refine_centroids(video_path, path, frame_w, frame_h)
    path = fill_every_frame(path)
    ok_flow, flow_note, flow_stats = validate_ball_path_on_video(video_path, path, frame_w, frame_h)
    if not ok_flow:
        return []
    path = annotate_frame_motion(path, fps, meters_per_pixel)
    if path:
        path[0]["flow_note"] = flow_note
        path[0]["flow_stats"] = flow_stats
    v_px = ballistic_release_speed_px_per_frame(path)
    if v_px is not None and meters_per_pixel:
        kmh = v_px * float(meters_per_pixel) * fps * 3.6
        if kmh < 22:
            return []
    else:
        speeds = [p.get("speed_kmh") for p in path if p.get("speed_kmh") is not None]
        if speeds and float(np.median(speeds[: min(12, len(speeds))])) < 22:
            return []
    return path


def _point(frame: int, c: dict[str, Any], source: str = "detected") -> dict[str, Any]:
    return {
        "frame": int(frame),
        "x": float(c["x"]),
        "y": float(c["y"]),
        "r": float(c.get("r") or 6),
        "source": source,
    }


def _greedy_chain(
    per_frame: list[dict[str, Any]],
    start_i: int,
    seed: dict[str, Any],
    max_step: float,
) -> list[dict[str, Any]]:
    path = [_point(per_frame[start_i]["frame"], seed)]
    vx = vy = 0.0
    have_v = False
    misses = 0
    last_r = float(seed.get("r") or 6)
    for item in per_frame[start_i + 1 :]:
        df_guess = max(1, int(item["frame"]) - int(path[-1]["frame"]))
        pred = (
            path[-1]["x"] + vx * df_guess,
            path[-1]["y"] + vy * df_guess,
        ) if have_v else (path[-1]["x"], path[-1]["y"])
        picked = None
        best = 1e18
        for c in item["candidates"]:
            d = float(np.hypot(c["x"] - pred[0], c["y"] - pred[1]))
            step_lim = max_step * (1.6 if have_v else 2.4)
            if d > step_lim:
                continue
            r = float(c.get("r") or 6)
            if last_r >= 10 and (r < last_r * 0.35 or r > last_r * 2.8):
                continue
            cost = d + 0.15 * abs(r - last_r)
            if cost < best:
                best = cost
                picked = c
        if picked is None:
            misses += 1
            if misses >= 4:
                break
            continue
        misses = 0
        df = max(1, int(item["frame"]) - int(path[-1]["frame"]))
        vx = (picked["x"] - path[-1]["x"]) / df
        vy = (picked["y"] - path[-1]["y"]) / df
        have_v = True
        last_r = float(picked.get("r") or last_r)
        path.append(_point(item["frame"], picked))
    return path


def _best_flight_path(
    per_frame: list[dict[str, Any]],
    hand_x: float,
    hand_y: float,
    dx: float,
    dy: float,
    fps: float,
    mpp: float | None,
    frame_w: int,
    frame_h: int,
) -> list[dict[str, Any]]:
    """Pick the moving downrange object — not the hand, not a tree."""
    max_step = _max_step_px(fps, mpp, frame_w, frame_h)
    seed_horizon = min(len(per_frame), max(8, int(round(fps * 0.18))))
    raw: list[tuple[float, int, dict[str, Any]]] = []
    for i, item in enumerate(per_frame[:seed_horizon]):
        sky = [c for c in item["candidates"] if c["y"] < frame_h * 0.48]
        ranked = sorted(item["candidates"], key=lambda c: c.get("r") or 0, reverse=True)
        pool = ranked[:4] + sorted(sky, key=lambda c: c.get("r") or 0, reverse=True)[:3]
        seen_local = set()
        for c in pool:
            key = (int(c["x"]) // 25, int(c["y"]) // 25)
            if key in seen_local:
                continue
            seen_local.add(key)
            down = (c["x"] - hand_x) * dx + (c["y"] - hand_y) * dy
            dh = float(np.hypot(c["x"] - hand_x, c["y"] - hand_y))
            if down < 20 or c["y"] > frame_h * 0.65:
                continue
            if dh < 36:
                continue
            air = max(0.0, hand_y - c["y"])
            score = float(c.get("r") or 4) * (1.0 + air / 80.0)
            raw.append((score, i, c))
    raw.sort(key=lambda t: t[0], reverse=True)
    seeds: list[tuple[float, int, dict[str, Any]]] = []
    used = set()
    for item in raw:
        key = (int(item[2]["x"]) // 40, int(item[2]["y"]) // 40)
        if key in used:
            continue
        used.add(key)
        seeds.append(item)
        if len(seeds) >= 24:
            break

    best: list[dict[str, Any]] = []
    best_score = -1.0
    for _, i, c in seeds:
        chain = _greedy_chain(per_frame, i, c, max_step)
        if len(chain) < 6:
            continue
        net = _dist(chain[0], chain[-1])
        if net < 70:
            continue
        steps = [
            _dist(a, b) / max(1, int(b["frame"]) - int(a["frame"]))
            for a, b in zip(chain, chain[1:])
        ]
        med_step = float(np.median(steps)) if steps else 0.0
        span = max(1, int(chain[-1]["frame"]) - int(chain[0]["frame"]))
        if med_step < 3.0 or (net / span) < 3.0:
            continue
        move = (chain[-1]["x"] - chain[0]["x"], chain[-1]["y"] - chain[0]["y"])
        mag = float(np.hypot(move[0], move[1])) or 1.0
        align = (move[0] * dx + move[1] * dy) / mag
        if align < 0.15:
            continue
        sc = net * med_step * len(chain) * (0.4 + 0.6 * max(0.0, align))
        if sc > best_score:
            best = chain
            best_score = sc
    return best


def _ballistic_clean(path: list[dict[str, Any]], frame_w: int, frame_h: int) -> list[dict[str, Any]]:
    """Drop teleport / wrong-blob points that don't fit one flight parabola."""
    if len(path) < 6:
        return path
    ts = np.array([p["frame"] for p in path], dtype=float)
    xs = np.array([p["x"] for p in path], dtype=float)
    ys = np.array([p["y"] for p in path], dtype=float)
    try:
        cx = np.polyfit(ts, xs, 1)
        cy = np.polyfit(ts, ys, 2 if len(path) >= 6 else 1)
    except Exception:
        return path
    tol = max(28.0, 0.018 * float(np.hypot(frame_w, frame_h)))
    kept = []
    for p in path:
        d = float(np.hypot(p["x"] - np.polyval(cx, p["frame"]), p["y"] - np.polyval(cy, p["frame"])))
        if d <= tol:
            kept.append(p)
    return kept if len(kept) >= 6 else path


def _dark_centroid(bgr: np.ndarray, x: float, y: float, rad: float) -> tuple[float, float] | None:
    """Sub-pixel centre of the dark blob around a lock — cuts per-frame jitter."""
    h, w = bgr.shape[:2]
    r = int(max(8.0, rad))
    x0, y0 = max(0, int(x) - r), max(0, int(y) - r)
    x1, y1 = min(w, int(x) + r), min(h, int(y) + r)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    gray = cv2.cvtColor(bgr[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    m = cv2.moments(mask)
    if m["m00"] < 16:
        return None
    cx = float(m["m10"] / m["m00"]) + x0
    cy = float(m["m01"] / m["m00"]) + y0
    if float(np.hypot(cx - x, cy - y)) > rad * 1.15:
        return None
    return cx, cy


def _refine_centroids(video_path, path: list[dict[str, Any]], frame_w: int, frame_h: int) -> list[dict[str, Any]]:
    from app.pipeline import extract

    det = {int(p["frame"]): p for p in path if p.get("source") == "detected"}
    if len(det) < 4:
        return path
    f0, f1 = min(det), max(det)
    for idx, bgr in extract.iter_frame_range(video_path, f0, f1):
        p = det.get(idx)
        if p is None:
            continue
        hit = _dark_centroid(bgr, p["x"], p["y"], max(10.0, float(p.get("r") or 10) * 1.6))
        if hit is None:
            continue
        p["x"], p["y"] = float(hit[0]), float(hit[1])
    return path


def _init_tracker(bgr: np.ndarray, point: dict[str, Any]):
    maker = getattr(cv2, "TrackerCSRT_create", None)
    if maker is None:
        legacy = getattr(cv2, "legacy", None)
        maker = getattr(legacy, "TrackerCSRT_create", None) if legacy is not None else None
    if maker is None:
        return None
    r = max(8.0, float(point.get("r") or 6))
    x = int(point["x"] - r)
    y = int(point["y"] - r)
    w = int(r * 2)
    h = int(r * 2)
    ih, iw = bgr.shape[:2]
    x = max(0, min(x, iw - 4))
    y = max(0, min(y, ih - 4))
    w = max(8, min(w, iw - x))
    h = max(8, min(h, ih - y))
    tr = maker()
    try:
        tr.init(bgr, (x, y, w, h))
        return tr
    except Exception:
        return None


def fill_every_frame(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Insert a breakpoint on every missing frame using a ballistic fit."""
    if len(points) < 3:
        return points
    pts = sorted(points, key=lambda p: p["frame"])
    by_f = {int(p["frame"]): p for p in pts}
    f0, f1 = int(pts[0]["frame"]), int(pts[-1]["frame"])
    ts = np.array([p["frame"] for p in pts], dtype=float)
    xs = np.array([p["x"] for p in pts], dtype=float)
    ys = np.array([p["y"] for p in pts], dtype=float)
    try:
        cx = np.polyfit(ts, xs, 1)
        cy = np.polyfit(ts, ys, 2 if len(pts) >= 4 else 1)
    except Exception:
        return pts
    filled = []
    for f in range(f0, f1 + 1):
        if f in by_f:
            filled.append(by_f[f])
            continue
        # Blend radius from neighbours.
        r0 = float(pts[0].get("r") or 4)
        r1 = float(pts[-1].get("r") or 4)
        t = (f - f0) / max(1, f1 - f0)
        filled.append({
            "frame": f,
            "x": float(np.polyval(cx, f)),
            "y": float(np.polyval(cy, f)),
            "r": float(r0 * (1 - t) + r1 * t),
            "source": "interpolated",
        })
    return filled


def annotate_frame_motion(
    points: list[dict[str, Any]],
    fps: float,
    meters_per_pixel: float | None,
) -> list[dict[str, Any]]:
    """Attach distance and speed between consecutive breakpoints."""
    out: list[dict[str, Any]] = []
    prev = None
    for p in sorted(points, key=lambda d: d["frame"]):
        q = dict(p)
        if prev is not None:
            df = max(1, int(q["frame"]) - int(prev["frame"]))
            dist_px = float(np.hypot(q["x"] - prev["x"], q["y"] - prev["y"]))
            q["dx_px"] = float(q["x"] - prev["x"])
            q["dy_px"] = float(q["y"] - prev["y"])
            q["dist_px"] = dist_px
            q["dt_s"] = df / max(fps, 1e-6)
            q["speed_px_per_frame"] = dist_px / df
            if meters_per_pixel:
                q["dist_m"] = dist_px * float(meters_per_pixel)
                q["speed_mps"] = q["dist_m"] / q["dt_s"]
                q["speed_kmh"] = q["speed_mps"] * 3.6
        out.append(q)
        prev = q
    return out


def ballistic_release_speed_px_per_frame(ball_track: list[dict[str, Any]]) -> float | None:
    """Release speed from a ballistic fit on detected in-air points.

    x(t) = x0 + vx·t, y(t) = y0 + vy·t + a·t². Instantaneous speed at the first
    in-air sample is hypot(vx, dy/dt). This averages centroid jitter that
    inflates per-frame distance ÷ time.
    """
    pts = sorted(
        [p for p in ball_track if p.get("source") != "interpolated"],
        key=lambda p: p["frame"],
    )
    if len(pts) < 5:
        pts = sorted(ball_track, key=lambda p: p["frame"])
    if len(pts) < 5:
        return flight_release_speed_px_per_frame(ball_track)
    t0 = int(pts[0]["frame"])
    # First half of the detected flight (min 8 frames) — closest to release.
    early = [p for p in pts if int(p["frame"]) - t0 <= max(8, int(round((pts[-1]["frame"] - t0) * 0.45)))]
    if len(early) < 5:
        early = pts[: max(5, min(12, len(pts)))]
    ts = np.array([float(p["frame"]) for p in early], dtype=float)
    xs = np.array([float(p["x"]) for p in early], dtype=float)
    ys = np.array([float(p["y"]) for p in early], dtype=float)
    if ts[-1] <= ts[0]:
        return None
    try:
        cx = np.polyfit(ts, xs, 1)
        cy = np.polyfit(ts, ys, 2 if len(early) >= 6 else 1)
    except Exception:
        return None
    vx = float(cx[0])
    inst = []
    for t in ts:
        vy = float(2 * cy[0] * t + cy[1]) if len(cy) == 3 else float(cy[0])
        inst.append(float(np.hypot(vx, vy)))
    v = float(np.median(inst))
    return v if v > 0.4 else None


def flight_release_speed_px_per_frame(ball_track: list[dict[str, Any]]) -> float | None:
    """Release speed = robust mean of per-frame distance/time at the start of flight."""
    pts = sorted(ball_track, key=lambda p: p["frame"])
    speeds = [float(p["speed_px_per_frame"]) for p in pts if p.get("speed_px_per_frame")]
    if len(speeds) >= 3:
        # First ~12 intervals after the ball is moving; median kills a single jump.
        window = speeds[: min(12, len(speeds))]
        return float(np.median(window))
    if len(pts) < 4:
        return None
    n = min(10, len(pts))
    ts = np.array([p["frame"] for p in pts[:n]], dtype=float)
    xs = np.array([p["x"] for p in pts[:n]], dtype=float)
    ys = np.array([p["y"] for p in pts[:n]], dtype=float)
    if ts[-1] <= ts[0]:
        return None
    try:
        cx = np.polyfit(ts, xs, 1)
        cy = np.polyfit(ts, ys, 1)
    except Exception:
        return None
    v = float(np.hypot(cx[0], cy[0]))
    return v if v > 0.3 else None


def _max_step_px(fps: float, mpp: float | None, frame_w: int, frame_h: int) -> float:
    """Max plausible ball displacement per frame (~50 m/s / 180 km/h)."""
    diag = float(np.hypot(frame_w, frame_h)) or 1.0
    cap = diag * 0.12
    if mpp and mpp > 0 and fps > 1:
        return float(min(cap, 50.0 / (mpp * fps)))
    return float(min(cap, max(18.0, min(frame_w, frame_h) * 0.08)))


def _pick_candidate(
    cands,
    px,
    py,
    search_r,
    max_step,
    hand_xy: tuple[float, float] | None = None,
    min_hand_dist: float = 0.0,
) -> dict[str, Any] | None:
    best = None
    best_cost = 1e9
    for c in cands:
        if float(c.get("r") or 0) > 70:
            continue
        d = float(np.hypot(c["x"] - px, c["y"] - py))
        if d > search_r or d > max_step:
            continue
        if hand_xy is not None and min_hand_dist > 0:
            dh = float(np.hypot(c["x"] - hand_xy[0], c["y"] - hand_xy[1]))
            if dh < min_hand_dist:
                continue
        cost = d / (0.35 + float(c.get("score") or 0.5))
        if cost < best_cost:
            best_cost = cost
            best = c
    return best
