# TASK-007 — Slow-motion timebase, ball recovery, precise release

**Feature:** FEAT-007 (metrics), FEAT-014 (ball), FEAT-005 (release), FEAT-012 (PDF)
**Status:** Done
**Priority:** P0
**Date:** 24 Aug 2026

## Why

A user clip (`vid_d6fdd6b445f5.mov`, night nets, white ball) reported ball speed
**61.9 km/h against arm speed 70.6 km/h** — physically impossible, since the ball
leaves from beyond the wrist on the same arm. The bowler put the delivery above
100 km/h. Release was also firing ~3 frames early, and the ball was not tracked
at all on the first pass.

Root cause was not the tracker. **The clip is a 4x slow-motion export: the
container says 30 fps, the capture was 120 fps.** Nothing in the file says so,
so every speed read 4x too slow and every phase window was cut four times too
narrow in frames.

## What changed

- **`pipeline/timebase.py` (new)** — recovers the real capture rate from gravity.
  A ball in free flight accelerates at `a_px = g / (mpp · fps²)`, so a parabola on
  the tracked path plus the height scale solves for fps. Guards: ≥8 detected
  in-air points, ≥8 frames span, ≥3σ on the quadratic term, result in 20-600 fps,
  and the container rate must be understated by ≥1.5x before anything is overridden.
  - Two curvature readings with **known, opposite biases** are reconciled rather
    than averaged. A quadratic over the whole arc is biased low (implied fps
    biased *high*) because the ball recedes and its pixels come to be worth more
    metres; a cubic absorbs that drift, leaving the quadratic term as curvature
    at release — unbiased but noisier. The arc value is used as an upper bound,
    the release value as the centre. The 3σ gate is **either** reading: a noisy
    cubic (significance 2.6) with a clean arc (14σ) is still a real parabola;
    requiring the cubic alone left 30 fps slow-mo exports reading 4× too slow.
  - Candidate rates are **whole multiples of the container rate** (a slow-motion
    export writes one captured frame per container frame). On the test clip:
    arc 146.7, release 105.0 → the fastest multiple under the bound and within
    30% of the centre is **120** = exactly 4x. Snapping to a generic list of
    "camera rates" instead picked 100 fps, which a 30 fps container cannot produce.
- **`pipeline/runner.py`** — measures the timebase after ball tracking, then
  re-runs the action pass on the corrected rate. The ball path is indexed by
  frame, so it is **not** re-tracked; it is re-annotated with per-point km/h at
  the corrected rate (otherwise the two ball-speed estimators disagree by exactly
  the slow-motion factor and the disagreement guard vetoes a good measurement).
- **`pipeline/action.py`**
  - `ball_leave_frame()` split out of `snap_release_to_ball_leave()`, and
    `analyze_action(release_override=...)` added. Release now comes from the ball
    where one is tracked, and **every other event is searched relative to that
    measured release** instead of a wrist-speed heuristic. Release on the test
    clip: 58 → 65 (ball first airborne at 64).
  - Foot contact rewritten around `_plant_frame()`: the first frame of the final
    plateau — when the ankle reached the height it holds through release. The old
    "hardest downward strike in the window" picked a *run-up* stride once the
    window widened at 120 fps (FFC landed at frame 14 instead of 51). FFC gap to
    release relaxed from 120-550 ms to **60-600 ms**; the 120 ms floor excluded
    legitimately quick actions — this clip's real FFC→REL is 125 ms.
- **`pipeline/track.py`**
  - **x(t) is fitted as a quadratic, not a line** (`_fit_xy`), everywhere. Image-x
    decelerates as the ball recedes; a linear fit mis-predicts the frames nearest
    release by tens of pixels, and `_ballistic_clean` was discarding exactly those
    frames as outliers. This single assumption caused most of the speed error.
  - Release speed (`ballistic_release_speed_px_per_frame`) is now the derivative
    **at the first in-air sample** over a ~0.12 s window, bounded against the raw
    early steps. It was the median over half the arc — 27.8 px/frame where release
    was 39.5.
  - Dual path generators (greedy chain + ballistic RANSAC), each carried through
    optical-flow validation independently; the better **surviving** one wins. A
    single favourite lost the real flight when it failed. Candidates must start
    within 0.15 s of release, which stops RANSAC preferring a long clean path in
    the background after the ball has left frame.
  - `_reassociate()` re-picks every frame against the fitted parabola;
    `_longest_continuous()` cuts teleports onto floodlight flares;
    `_ballistic_clean()` now rejects on **residual MAD** rather than a fixed
    tolerance, and runs again *after* re-association (a fixed tolerance was wide
    enough to admit a second blob and the track alternated between the two).
  - The `align` gate now looks at the first ~8 frames only — it compares against
    the wrist's own velocity, which points nearly straight up at release.
  - The terminal `kmh < 22` reject is gone: at that point `fps` is still the
    container rate, so it discarded exactly the slow-motion flights the timebase
    needs. Speed sanity now lives in metrics, after the timebase is settled.
  - **Follow-up (native 120 fps 1080p, `vid_d6bffa06874c`):** a turf cut at 72%
    of frame height deleted the white ball the moment it left the hand (bowler
    sits in the lower half). Turf is now relative to the wrist. `max_start` is
    capped in frames *and* the first point must be near the hand (kills poster
    RANSAC). Optical flow is validated on detected samples only. Native-120
    leave-hand search is capped so REL cannot walk 0.32 s into follow-through.
- **`pipeline/metrics.py`**
  - A validated in-air flight now **overrides the pose-based view guess** for
    speed. The classifier read this mixed action as front-on (shoulder ratio
    0.209 vs a 0.20 threshold) and refused every speed on a clip whose ball
    plainly crosses the frame.
  - Release height no longer gated on a side-on view — it is a *vertical*
    distance, and camera yaw does not foreshorten vertical pixels.
  - `speed_consistency`: ball speed below arm speed is flagged as impossible and
    the ball figure marked a lower bound, rather than shown as-is.
  - `_robust_ball_kmh` cross-check window is **time-based (~0.12 s)**, matching
    the fit's window, so it describes the same instant. Over the whole flight it
    reads far below a correct fit on any receding delivery and vetoed good
    measurements; over a fixed sample count it is the contaminated half on a
    200 fps clip.
  - New `delivery_type` (pace band from measured ball speed, falling back to arm
    speed with the weaker basis stated) and `action_legality`.
- **`pdf/report.py`** — page 6 gains a DELIVERY & ARM ACTION block: pace band and
  its basis, elbow angle at release, elbow extension, throwing screening verdict,
  and the capture rate **with its source**, since every speed above it is divided
  by that number.

## Throwing (chuck) screening — deliberately conservative

A first cut reported "over the ICC 15° limit" on three normal bowlers (69°, 87°,
118°). It was measuring from `arm_horizontal`, which uses shoulder→**wrist** and
can fire mid-upswing where the elbow is deeply cocked. Measuring from the true
upper-arm-horizontal instant instead, the 2D elbow readings across clips ranged
76°-177° — projection noise, not flexion.

**A single camera cannot support an ICC verdict**, and shipping one would accuse a
legal bowler of throwing. So: elbow angle at release is always reported; the
extension and a verdict only where the view is genuinely side-on, the upper arm
passes within 20° of horizontal in the second half of the FFC→release interval,
and the arm projects to a real length at both ends. Otherwise `verdict: None` with
the reason. Verdicts are worded as screening (`within_limit`, `borderline`,
`above_limit_screening`), never as a call.

## Verified

| Clip | fps | Ball | Arm | Ratio | REL time | Notes |
|------|-----|------|-----|-------|----------|-------|
| `vid_d6fdd6b445f5.mov` (user) | 30→**120** measured | **84.1** (was 61.9) | 68.9 | 1.22 | 125 ms | REL 58→65, FFC 51, BFC 36 |
| `vid_d6bffa06874c.mov` (native 120, 1080p) | 120 (confirmed) | **82.9** | 52.6 | 1.58 | 100 ms | was 0 ball pts: turf cut at 72% of frame deleted the white ball; REL walked into FT (wrist on hip → 0.63 m height reject). REL 150→136 |
| `vid_d899e3def0cd.mp4` | 203 (confirmed) | 63.8 | 55.3 | 1.15 | 84 ms | was 85.8 from a track alternating between two blobs |
| `vid_853d1336e422.mp4` | 120 (confirmed) | 74.0 | 47.4 | 1.56 | 133 ms | |
| `vid_d8baecfa0b70.mp4` | 149 (confirmed) | — | 45.8 | — | 134 ms | no clean flight; honest `—` |
| `vid_193e826c26f6.mp4` | 30 (confirmed) | — | — | — | — | front-on portrait; honestly refused |

Ball speed exceeds arm speed on every clip that reports both — the check that
started this task.

## Honest limits

- The reported ball speed is the component **across the image**. A single camera
  cannot see motion along its own axis, so on anything but a square-on view the
  true release speed is higher. On the user's clip the ball recedes to ~1.5x its
  release distance during the tracked flight, so the real figure is meaningfully
  above 84 km/h — consistent with the bowler's own estimate of 100+. Reported as
  a floor, with the recession factor in the note; closing that gap needs a
  square-on camera or a second view (FEAT-016).
- Gravity-based fps needs a tracked flight. Without a ball the container rate
  stands, so a slow-motion clip with no visible ball still mis-times. Stated in
  the timebase note.
- 2D hip/trunk proxies remain unreliable on open-chested actions and are refused
  more often at 120 fps, where angular velocities scale up fourfold.
