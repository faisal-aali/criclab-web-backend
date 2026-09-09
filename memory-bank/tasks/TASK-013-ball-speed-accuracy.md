# TASK-013 — Ball-speed accuracy and overlay legibility

**Feature:** FEAT-029 (ball-speed correctness), FEAT-030 (overlay legibility)
**Status:** Both done and validated on the real clip. The follow-up slow-motion
/ breakpoints request is **not started** — see the last section.
**Priority:** P0
**Date:** 27 Aug 2026

## The report, and why the premise was wrong

Reported as: *"the ball speed appears to be around 131 km/h, while the
application is displaying something closer to 100 km/h."*

Investigated against the actual source clip (`del_4c16280fbefa`,
`vid_ea358935d906.mov`, 3840×2160 @ 119.946 fps) rather than adjusting the
displayed value. **Neither number was correct.** The true image-plane release
speed is **86 km/h**, and 100 and 131 are two different symptoms of one bug.

The decisive evidence was in the screenshot itself: the overlay labelled the
ball **133 km/h** while its own metrics panel read **100 km/h** on the same
frame. One render disagreeing with itself is a calculation problem, not a
display problem.

### Root cause: the y-axis is noise, and `hypot` cannot tell

Frame-to-frame deltas over the detected flight (frames 186–197):

```
dx:  100.6  95.1  95.1  101.7  98.1  102.9  94.4  118.7  89.1  99.5  105.5   <- clean
dy:    3.6   9.2  16.9   86.8   2.9    5.1   7.9   33.0  38.2 -28.1   14.9   <- impossible
```

A ball in free flight has smooth `dy` increasing by a constant. At this scale
that constant is `9.81/(mpp·fps²) = 0.343 px/frame²`. Observed jumps of
16.9 → 86.8 → 2.9, and a *negative* value, are the detector's centroid sliding
along a 100+ px motion-blur streak at 4K — not motion.

Residual RMS against a fitted path: **x = 5.1 px, y = 19.0 px.**

Everything else followed from that:

* **`hypot(dx, dy)` turned y-noise into speed.** Every frame reporting 130+
  km/h was a y-spike, including the 132.51 at frame 186 that the overlay
  printed. Raw per-frame values swung **81–133 km/h** on a ball travelling at
  a steady ~100 px/frame.
* **Two estimators fed the same corrupted signal cannot cross-check.** The old
  code computed a quadratic endpoint-derivative (104.9 km/h) and the median of
  raw per-frame speeds (88.5 km/h), rejected the pair if they disagreed by
  >35%, and otherwise blended 70/30 → **100.0 km/h**. Both inputs were inflated
  by the same noise, so their "agreement" carried no information.
* **Least squares could not resist a streak outlier.** The first detected point
  (frame 183) sits 124 px off the path in x while its y matches — a classic
  streak-centroid error. It dragged the whole fit: residuals went
  6.7 px → 28.2 px, and median-based trimming failed because the outlier had
  already bent the line it was being measured against.
* **The same noise corrupted the timebase.** Apparent gravity measured
  **11.62 px/frame²** against a physical **0.343** (34× too large), so the
  gravity-derived frame rate came out **20.6 fps** on a 120 fps clip
  (`raw_measured_fps: 20.6` in the stored timebase). It fell back to the
  container rate, which happened to be right — but for no good reason.

## The fix — `track.robust_release_velocity_px_per_frame`

One estimator, constrained by physics, replacing the two that disagreed:

1. **Pin gravity rather than fit it.** Once `fps` and `mpp` are known, vertical
   acceleration in pixels is not a free parameter. Subtract `0.5·g·t²` from y
   first; what remains is a straight line whose slope is vertical velocity *and
   nothing else*, so a y-outlier can no longer be absorbed as acceleration —
   i.e. can no longer become speed.
2. **Theil–Sen instead of least squares.** Median of all pairwise slopes. An
   outlier corrupts only the `n−1` pairs containing it out of `n(n−1)/2`, so
   the median does not move. O(n²) over ≤15 points is free.
3. **Never fit interpolated points.** `interpolate_gaps` fabricates positions
   to keep the drawn path continuous — a drawing aid. Fitting them feeds the
   estimator its own guesses back as evidence. (Frames 184–185 were
   interpolated and were being given "measured" speeds of 130.66 and 102.07.)
4. **Refuse rather than guess.** The fit reports a `quality` from its own
   residuals; below `MIN_BALL_FIT_QUALITY = 0.25` the metric goes
   `unavailable` with a reason instead of a number.

### Result on the reported clip

```
ball-speed | fps=119.946 mpp=0.0019863 g_px=0.3432/frame^2 |
release_frame=180 release_t=1.5007s fit_window=183-194
points_used=9 dropped=1 | resid x=6.7px y=19.6px |
vx=98.62 vy=18.88 |v|=100.41 px/frame |
vx=84.6 vy=16.2 release_speed=86.1 km/h | quality=0.41 -> REPORTED
```

86.12 km/h, with the bad frame-183 point correctly dropped and the x-residual
back to 6.7 px. Cross-checked three independent ways, all agreeing:
Theil–Sen fit **86.1**, x-only linear fit + physical gravity **85.9**,
raw hypot median **86.3**.

### The quality gate earning its place immediately

Re-running six stored deliveries through the new estimator:

| delivery | was | now | quality |
|---|---|---|---|
| `del_4c16280fbefa` | 100.01 | **86.1** | 0.41 |
| `del_7033a8ea4e5d` | 98.59 | 98.2 | 0.88 |
| `del_cb41eb5590aa` | 91.86 | 87.4 | 0.96 |
| `del_30afcbac31ec` | 101.42 | 101.0 | 0.88 |
| `del_d860f3ea5dba` | 102.88 | 88.6 | 0.41 |
| `del_236f3d0e3bd8` | 63.24 | **refused** | 0.00 |

The last row matters most: that track's fitted horizontal velocity is
*negative* (−34.1 km/h — the "ball" moves backwards). It had been displaying
63.24 km/h with confidence. It is now `unavailable` with a reason.

Clean clips barely moved (98.59 → 98.2, 101.42 → 101.0), which is the right
signature — this corrects noisy tracks without disturbing good ones.

## Overlay legibility

* Trails, skeleton, phase beads and the ball ring are now drawn onto a scratch
  copy and `addWeighted` back (`TRAIL_ALPHA 0.55`, `SKELETON_ALPHA 0.6`,
  `MARKER_ALPHA 0.75`), so the footage stays readable through them.
* Widths roughly halved: hand trail 11 → 5, ball trail 6 → 3, skeleton 3/2 → 3/2
  with 3 px joints instead of 5 px double-stroked circles, halo +6 → +3.
* Blended **per layer, not per shape** — per-shape alpha compounds where limbs
  overlap and reads opaque at exactly the joints.
* **The per-frame ball label is gone.** It now shows the one measured release
  speed (`"86 km/h at release"`). A number that swings 81–133 km/h on noise is
  not a measurement worth showing, and printing it next to a differently
  computed headline is what made this bug visible in the first place.

Verified by rendering the real clip and inspecting frames — netting and body
are visible through the trail, panel reads 86 KM/H.

## Debug trail

Every reported speed logs one line under `criclab.metrics` (format above) with
fps, mpp, gravity, release frame and timestamp, fit window, points used and
dropped, x/y residuals, vx/vy in both px/frame and km/h, quality, and whether
it was REPORTED or REFUSED. Logged from the same values the metric is built
from — not recomputed — so it can never describe a different calculation than
the one that produced the displayed number.

## Removed

`_robust_ball_kmh` (metrics.py) and the imports of
`ballistic_release_speed_px_per_frame` / `flight_release_speed_px_per_frame`.
Leaving two competing ways to compute one number is how the original bug
survived. The functions remain in `track.py` (still used internally by each
other) but nothing in the metrics path calls them.

## A related discrepancy noticed but NOT resolved

The stored release point (frame 180, y=1223) sits **426 px below** the first
ball detection (frame 183, y=797). Three frames at ~100 px/frame cannot cover
that vertically — it implies ~137× gravity. So release detection and the start
of the ball track disagree about where the ball left the hand.

It does not affect the reported speed (the fit starts from detected flight
points and the outlier is dropped), but it means the drawn path shows a
vertical jump from the REL marker to the ball, and `release_height_m` may be
measuring from the wrong instant. Worth a separate look.

## NOT started — the follow-up request

A second request arrived mid-task covering slow-motion playback and
breakpoints. None of it is built:

1. **120 → 30 fps playback (4× slow motion).** `render.py` currently does the
   opposite on purpose: `out_fps = 30.0` with `rate_out = out_fps / in_fps`
   deliberately normalising high-fps clips back to ~real time with only a 2×
   window around the delivery (the comment says "never everything ×7 slower on
   high-fps clips"). Changing this is a deliberate reversal of an existing
   design decision, not a bug fix — worth confirming the intent before doing it.
2. **Breakpoints.** Largely *already computed* — `metrics.phases`,
   `phase_labels`, `phase_sources`, `event_t_ms` and `release_frame` exist for
   BFC / FFC / hip rotation / MER / arm horizontal / release / follow-through.
   What is missing is exposing each as `{event, source_frame, source_time,
   playback_time, confidence}` and the UI to click-and-seek. The spec also asks
   for Start / Run-up / Gather / Post-release / End, which are **not** currently
   detected.
3. **Dual timeline.** Source frame + source time + playback time carried
   together, so an analysis timestamp is never confused with a displayed one.
4. **Player controls** (0.25×–4×) and a "Source: 4K • 120 FPS / Playback: 30 FPS
   • 4× Slow Motion" readout — frontend, not started.

Note the request's point 8 ("slow motion must not change the calculated ball
speed") is **already satisfied**: physics uses `fps` from `extract_video_meta`
corrected by `pipeline/timebase.py`, and the render's `out_fps` is a separate
value that never reaches the metrics path.
