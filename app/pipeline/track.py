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


def _fit_xy(ts: np.ndarray, xs: np.ndarray, ys: np.ndarray):
    """Fit the flight's image path: x AND y quadratic in frame index.

    x(t) is often modelled as a straight line, on the reasoning that horizontal
    velocity is constant. That holds in the world, not in the image: a delivery
    travels downrange, away from the lens, so its pixels-per-frame decay through
    the flight. Fitting x linearly mis-predicts the frames nearest release by
    tens of pixels — which is exactly where release speed is read — and an
    outlier filter built on it throws that early flight away.
    """
    deg = 2 if len(ts) >= 6 else 1
    t = ts - float(ts[0])
    return np.polyfit(t, xs, deg), np.polyfit(t, ys, deg), float(ts[0])


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

    start = max(0, int(release_frame) - 8)
    # Scan a generous span of frames, not a span of seconds: `fps` here is the
    # container rate, which a slow-motion export understates several-fold, and a
    # short scan yields too few in-air samples for either the speed fit or the
    # gravity check in app.pipeline.timebase. Extra frames only cost decode time.
    end = int(release_frame) + min(150, max(40, int(round(fps * 0.50))))
    hand_x, hand_y = float(wrist_xy[0]), float(wrist_xy[1])
    dx, dy = (1.0, -0.35)
    if throw_dir is not None:
        n = float(np.hypot(throw_dir[0], throw_dir[1])) or 1.0
        dx, dy = float(throw_dir[0]) / n, float(throw_dir[1]) / n
    # Elbow→wrist at cocking points at the sky. A cricket ball leaves along the
    # pitch, so a near-vertical "throw" would reject the real in-air path.
    if abs(dy) > abs(dx) * 1.25:
        dx = 1.0 if dx >= 0 else -1.0
        dy = -0.18
        n = float(np.hypot(dx, dy)) or 1.0
        dx, dy = dx / n, dy / n

    per_frame: list[dict[str, Any]] = []
    prev_gray = None
    for idx, bgr in extract.iter_frame_range(video_path, start, end):
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        elapsed = max(0, idx - int(release_frame))
        dark = detect.detect_dark_flight_candidates(bgr)
        color = detect.detect_ball_color_candidates(bgr)
        bright = detect.detect_bright_flight_candidates(bgr)
        motion = detect.detect_ball_motion_candidates(prev_gray, gray) if prev_gray is not None else []
        hough = hough_ball_candidates(bgr)
        cands = detect.merge_ball_candidates(dark, color, bright, motion, hough)
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
            if c["y"] > frame_h * 0.78:
                continue
            down = (c["x"] - hand_x) * dx + (c["y"] - hand_y) * dy
            if elapsed >= 4 and down < 8:
                continue
            filtered.append(c)
        per_frame.append({"frame": int(idx), "candidates": filtered})
        prev_gray = gray

    # Two independent ways to find the flight, because each fails differently.
    # Greedy chaining follows the nearest candidate and can be captured by a big
    # arm or torso blob near release; ballistic RANSAC is order-independent and
    # scores whole hypotheses by how many candidates fit one parabola, so it
    # survives the ball being missed on a few frames. Both are carried through to
    # optical-flow validation and the better *surviving* one wins — picking a
    # single favourite up front loses the real flight when that favourite fails.
    seeds: list[list[dict[str, Any]]] = []
    greedy = _best_flight_path(per_frame, hand_x, hand_y, dx, dy, fps, meters_per_pixel, frame_w, frame_h)
    if len(greedy) >= 6:
        seeds.append(greedy)
    ransac = build_trajectory(per_frame, frame_w, frame_h, min_len=6)
    if len(ransac) >= 6:
        seeds.append([_point(p["frame"], p) for p in ransac])
    if not seeds:
        return []

    # The ball leaves the hand at release. A path that only starts well after it
    # is something else in the scene that happens to move consistently — on a
    # 200 fps clip, RANSAC will happily find a long, clean, entirely wrong one
    # in the background once the real flight has left the frame.
    max_start = int(release_frame) + max(4, int(round(fps * 0.15)))

    best: list[dict[str, Any]] = []
    best_key: tuple[int, int] | None = None
    for seed in seeds:
        cand = _longest_continuous(seed)
        if len(cand) < 6:
            continue
        cand = _ballistic_clean(cand, frame_w, frame_h)
        if len(cand) < 6:
            continue
        # Once a clean parabola exists, re-pick every frame against that model:
        # the ball is the candidate on the curve, whatever else happened to be
        # closer to the previous point.
        cand = _reassociate(per_frame, cand, frame_w, frame_h)
        cand = _longest_continuous(cand)
        if len(cand) < 6:
            continue
        # The seed often latches a frame or two late (the hand is a bigger blob
        # than the ball). Walk the clean parabola back toward the hand — those
        # earliest samples are what release speed and release detection use.
        cand = _extend_backward(per_frame, cand, hand_x, hand_y, frame_w, frame_h)
        if int(cand[0]["frame"]) > max_start:
            continue
        cand = _refine_centroids(video_path, cand, frame_w, frame_h)
        cand = fill_every_frame(cand)
        ok_flow, flow_note, flow_stats = validate_ball_path_on_video(video_path, cand, frame_w, frame_h)
        if not ok_flow:
            continue
        # Longer flights measure gravity and speed better; on a tie take the one
        # that starts earlier, because release speed is read off the first frames.
        key = (len(cand), -int(cand[0]["frame"]))
        if best_key is None or key > best_key:
            best_key, best = key, cand
            best[0]["flow_note"] = flow_note
            best[0]["flow_stats"] = flow_stats
    if len(best) < 6:
        return []
    # No km/h floor here: `fps` at this point is the *container* rate, which a
    # slow-motion export understates by 4-8x. Gating on km/h would throw away
    # exactly the flights that app.pipeline.timebase needs to detect that, and a
    # genuine ball would be discarded as "too slow". Speed sanity belongs in
    # metrics, after the timebase is settled. What remains here is
    # fps-independent: the path must cross the image (view.flight_geometry_ok)
    # and move with the optical flow (checked above).
    return annotate_frame_motion(best, fps, meters_per_pixel)


def _longest_continuous(path: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the longest run with no teleport between consecutive samples.

    A ball's step between frames changes smoothly. A lock that jumps to a
    floodlight flare or a post moves hundreds of pixels in one frame while the
    real flight moved thirty — so the run is cut there rather than fitted
    through, which would bend the parabola and corrupt every value read off it.
    """
    if len(path) < 4:
        return path
    pts = sorted(path, key=lambda p: int(p["frame"]))
    steps = [
        _dist(a, b) / max(1, int(b["frame"]) - int(a["frame"]))
        for a, b in zip(pts, pts[1:])
    ]
    med = float(np.median(steps))
    limit = max(3.0 * med, 40.0)
    runs: list[list[dict[str, Any]]] = [[pts[0]]]
    for step, nxt in zip(steps, pts[1:]):
        if step <= limit:
            runs[-1].append(nxt)
        else:
            runs.append([nxt])
    return max(runs, key=len)


def _reassociate(
    per_frame: list[dict[str, Any]],
    path: list[dict[str, Any]],
    frame_w: int,
    frame_h: int,
    iterations: int = 2,
) -> list[dict[str, Any]]:
    """Re-pick one candidate per frame against the flight's own fitted model.

    Greedy chaining is sequential, so one wrong pick drags the rest. Fitting
    x(t) linear and y(t) quadratic to the surviving points and then choosing the
    candidate nearest that curve is order-independent: it recovers frames the
    chain skipped and drops the ones it took from the arm. Ball size is used as
    a tie-break, so a torso blob sitting on the curve loses to the small round
    thing beside it.
    """
    if len(path) < 5:
        return path
    tol = max(12.0, 0.009 * float(np.hypot(frame_w, frame_h)))
    current = sorted(path, key=lambda p: int(p["frame"]))
    for _ in range(max(1, iterations)):
        ts = np.array([float(p["frame"]) for p in current])
        xs = np.array([float(p["x"]) for p in current])
        ys = np.array([float(p["y"]) for p in current])
        if len(current) < 5 or ts[-1] <= ts[0]:
            return current
        try:
            cx, cy, t0 = _fit_xy(ts, xs, ys)
        except Exception:
            return current
        med_r = float(np.median([float(p.get("r") or 8) for p in current])) or 8.0
        picked: list[dict[str, Any]] = []
        for item in per_frame:
            fr = int(item["frame"])
            t = float(fr) - t0
            px, py = float(np.polyval(cx, t)), float(np.polyval(cy, t))
            best, best_cost = None, 1e18
            for c in item["candidates"]:
                d = float(np.hypot(c["x"] - px, c["y"] - py))
                if d > tol:
                    continue
                cost = d + 0.6 * abs(float(c.get("r") or med_r) - med_r)
                if cost < best_cost:
                    best_cost, best = cost, c
            if best is not None:
                picked.append(_point(fr, best))
        if len(picked) < 5:
            return current
        current = _largest_contiguous(picked, max_gap=3)
        if len(current) < 5:
            return sorted(path, key=lambda p: int(p["frame"]))
    return current


def _extend_backward(
    per_frame: list[dict[str, Any]],
    path: list[dict[str, Any]],
    hand_x: float,
    hand_y: float,
    frame_w: int,
    frame_h: int,
) -> list[dict[str, Any]]:
    """Prepend earlier detections that sit on the flight's own back-projection.

    Stops at the hand: a candidate within a hand's reach of the wrist is the
    hand (or the ball still in it), not a ball in flight.
    """
    if len(path) < 5:
        return path
    pts = sorted(path, key=lambda p: int(p["frame"]))
    ts = np.array([float(p["frame"]) for p in pts])
    xs = np.array([float(p["x"]) for p in pts])
    ys = np.array([float(p["y"]) for p in pts])
    try:
        cx, cy, t0 = _fit_xy(ts, xs, ys)
    except Exception:
        return path
    tol = max(14.0, 0.010 * float(np.hypot(frame_w, frame_h)))
    hand_reach = max(30.0, 2.0 * float(pts[0].get("r") or 12.0))
    by_frame = {int(item["frame"]): item["candidates"] for item in per_frame}
    first = int(pts[0]["frame"])
    added: list[dict[str, Any]] = []
    for fr in range(first - 1, first - 14, -1):
        cands = by_frame.get(fr)
        if not cands:
            break
        px, py = float(np.polyval(cx, fr - t0)), float(np.polyval(cy, fr - t0))
        if float(np.hypot(px - hand_x, py - hand_y)) < hand_reach:
            break  # back-projection has reached the hand — the ball was still held
        pick, best = None, tol
        for c in cands:
            d = float(np.hypot(c["x"] - px, c["y"] - py))
            if d < best:
                best, pick = d, c
        if pick is None:
            break
        if float(np.hypot(pick["x"] - hand_x, pick["y"] - hand_y)) < hand_reach:
            break
        added.append(_point(fr, pick))
    if not added:
        return pts
    return sorted(added + pts, key=lambda p: int(p["frame"]))


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
    # Seed in *frames*, not seconds of container fps — an early pose-REL sits at
    # cocking, and the white ball only becomes a free blob 8–20 frames later.
    seed_horizon = min(len(per_frame), max(24, int(round(fps * 0.35))))
    raw: list[tuple[float, int, dict[str, Any]]] = []
    for i, item in enumerate(per_frame[:seed_horizon]):
        mid = [c for c in item["candidates"] if c["y"] < frame_h * 0.70]
        ranked = sorted(item["candidates"], key=lambda c: c.get("score") or 0, reverse=True)
        compact = sorted(
            [c for c in item["candidates"] if float(c.get("r") or 0) <= min(frame_w, frame_h) * 0.03],
            key=lambda c: c.get("score") or 0,
            reverse=True,
        )
        pool = ranked[:4] + compact[:4] + sorted(mid, key=lambda c: c.get("r") or 0, reverse=True)[:3]
        seen_local = set()
        for c in pool:
            key = (int(c["x"]) // 25, int(c["y"]) // 25)
            if key in seen_local:
                continue
            seen_local.add(key)
            down = (c["x"] - hand_x) * dx + (c["y"] - hand_y) * dy
            dh = float(np.hypot(c["x"] - hand_x, c["y"] - hand_y))
            if down < 20 or c["y"] > frame_h * 0.72:
                continue
            if dh < 36:
                continue
            air = max(0.0, hand_y - c["y"])
            r = float(c.get("r") or 4)
            # Prefer cricket-ball sized movers over poster-sized blobs that happen
            # to sit downrange of the hand.
            size_fit = 1.0 if r <= 18 else max(0.25, 18.0 / r)
            score = float(c.get("score") or r) * (1.0 + air / 80.0) * size_fit
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
        # Alignment is measured over the first few frames only. `dx, dy` comes from
        # the bowling wrist's own velocity, which at release can point almost
        # straight up; judging a whole arc against it rejects perfectly good
        # flights. Over the arc's end the ball has also begun to fall, which drags
        # the direction further away. Here it only has to not travel backwards.
        head = chain[: min(len(chain), 8)]
        move = (head[-1]["x"] - head[0]["x"], head[-1]["y"] - head[0]["y"])
        mag = float(np.hypot(move[0], move[1])) or 1.0
        align = (move[0] * dx + move[1] * dy) / mag
        if align < -0.20:
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
        cx, cy, t0 = _fit_xy(ts, xs, ys)
    except Exception:
        return path
    tol = max(28.0, 0.018 * float(np.hypot(frame_w, frame_h)))
    kept = []
    for p in path:
        t = float(p["frame"]) - t0
        d = float(np.hypot(p["x"] - np.polyval(cx, t), p["y"] - np.polyval(cy, t)))
        if d <= tol:
            kept.append(p)
    return kept if len(kept) >= 6 else path


def _blob_centroid(bgr: np.ndarray, x: float, y: float, rad: float) -> tuple[float, float] | None:
    """Sub-pixel centre of the locked blob — dark *or* white cricket ball."""
    h, w = bgr.shape[:2]
    r = int(max(8.0, rad))
    x0, y0 = max(0, int(x) - r), max(0, int(y) - r)
    x1, y1 = min(w, int(x) + r), min(h, int(y) + r)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    gray = cv2.cvtColor(bgr[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    # White leather is a bright disc; a red/dark ball is the opposite. Otsu-inv
    # on a white ball walks the centroid onto a nearby shadow and kills the lock.
    if float(np.mean(gray)) >= 140:
        _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    else:
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
        hit = _blob_centroid(bgr, p["x"], p["y"], max(10.0, float(p.get("r") or 10) * 1.6))
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
        cx, cy, t0 = _fit_xy(ts, xs, ys)
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
            "x": float(np.polyval(cx, f - t0)),
            "y": float(np.polyval(cy, f - t0)),
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


def ballistic_release_speed_px_per_frame(
    ball_track: list[dict[str, Any]],
    fps: float | None = None,
) -> float | None:
    """Image-plane speed of the ball *at the moment it leaves the hand*.

    Two things make a naive fit under-read badly:

    1. The ball recedes from the camera, so its pixel speed decays along the
       flight — x(t) is not a straight line. Fitting x linearly over half the arc
       and taking the median returns the mid-flight speed, not the release speed.
       Here x and y are both fitted as quadratics and differentiated at the first
       in-air sample.
    2. The window must stay near release. We use the first ~0.12 s of flight,
       where the ball is still close to the bowler's own depth plane.

    The quadratic derivative at an endpoint is noise-sensitive, so it is bounded
    against the raw early per-frame steps and falls back to their median if the
    fit runs away.
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
    if fps and fps > 1:
        span = max(6, int(round(float(fps) * 0.12)))
        early = [p for p in pts if int(p["frame"]) - t0 <= span]
    else:
        early = []
    if len(early) < 6:
        early = pts[: max(6, min(12, len(pts)))]
    if len(early) < 5:
        return flight_release_speed_px_per_frame(ball_track)

    ts = np.array([float(p["frame"]) for p in early], dtype=float)
    xs = np.array([float(p["x"]) for p in early], dtype=float)
    ys = np.array([float(p["y"]) for p in early], dtype=float)
    if ts[-1] <= ts[0]:
        return None

    raw_steps = [
        _dist(a, b) / max(1, int(b["frame"]) - int(a["frame"]))
        for a, b in zip(early, early[1:])
    ]
    raw_med = float(np.median(raw_steps)) if raw_steps else None

    deg = 2 if len(early) >= 6 else 1
    try:
        cx = np.polyfit(ts - ts[0], xs, deg)
        cy = np.polyfit(ts - ts[0], ys, deg)
    except Exception:
        return raw_med if raw_med and raw_med > 0.4 else None

    def _slope_at_zero(c: np.ndarray) -> float:
        return float(c[-2]) if len(c) >= 2 else 0.0

    v = float(np.hypot(_slope_at_zero(cx), _slope_at_zero(cy)))
    if raw_med and raw_med > 0:
        # An endpoint derivative can bolt; the raw early steps cannot.
        if not (0.75 * raw_med <= v <= 1.35 * raw_med):
            v = raw_med
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
