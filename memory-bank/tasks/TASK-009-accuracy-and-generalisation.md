# TASK-009 — Pose-first analysis, cross-validation, robust timebase

**Feature:** FEAT-004 (pose), FEAT-007 (metrics), FEAT-014 (ball), FEAT-018 (capture rate)
**Status:** Done
**Priority:** P0
**Date:** 26 Aug 2026

## Why

Two problems, one cause. Clips filmed from behind the bowler showed **no ball at
all**, and the recovered capture rate on the reference clip **flipped between 90
and 120 fps** on a small change in the tracked points — a 33% swing in every
speed. Underneath both: measurements that were individually plausible but never
checked against each other, and thresholds that meant different things on
different footage.

## What changed

### Finding the ball and quoting its speed are now separate questions

`runner` deleted the whole ball track whenever `flight_geometry_ok` failed. That
gate answers "can this support a km/h", not "is this a ball" — so on any clip
shot from behind or down the pitch the path was thrown away, the overlay drew no
ball, release lost the one frame we had actually measured, and the gravity
timebase lost its only input.

`view.flight_is_trackable` now answers the first question (is this a real moving
object that left the hand) and keeps the path; `flight_geometry_ok` still gates
the *speed* inside metrics, with its own reason. A from-behind clip now tracks
its ball and honestly refuses the km/h.

### Pose informs the ball search

Pose already ran first; now the ball search uses it. `runner._body_segments`
builds the bowler's limbs and torso per frame and
`track.track_ball_from_release` rejects candidates lying on them. The bowler is
the largest source of false candidates — a forearm or thigh is a fast,
ball-sized, ball-coloured blob, and one capturing the chain is how a track ends
up measuring the arm. The bowling forearm is deliberately excluded from the map:
at release the real ball is right beside it.

### The capture rate no longer flips

Gravity gives `fps` only together with the ball's depth, and the two were being
separated by a cubic's third term — a weak signal read off a few frames, noisy
enough that 90 and 120 were both "within tolerance".

`timebase._depth_growth` measures the recession directly instead: a ball's
apparent radius is inversely proportional to its distance, so the ratio of its
size at the start of the flight to the end *is* the ratio of those distances.
That de-biases the arc-averaged fit, which is precise, rather than relying on the
cubic, which is not. The cubic remains the fallback when radii are unusable, with
a wider tolerance (`RATE_TOL_CUBIC`). Candidate rates stay whole multiples of the
container rate, now up to 32x so phone super-slow-motion (960 fps) is covered.

### Cross-validation across independently measured quantities

`metrics._cross_validate` compares things measured by different routes, because a
number can sit inside its own band and still be impossible next to its
neighbours:

| Check | Why it is independent |
|-------|----------------------|
| FFC → release, BFC → FFC | Pose timing ÷ the recovered capture rate |
| Ball speed ÷ hand speed | Flight vs pose track — must be ≥ 1 |
| Release height ÷ stature | Measured height vs the height used to scale |
| Hand speed vs arm-swing rate | `v = ω × arm length`, two separate chains |

Failing checks lower the confidence of what they touch and are reported; nothing
is corrected silently. The arm-swing check is the strongest: on the reference
clip the two chains agree to **1%** (ratio 1.01), which is independent evidence
that both the recovered frame rate and the pixel scale are right.

### Thresholds that meant different things on different footage

An audit found ~20 gates in absolute pixels or absolute pixels-per-frame. A pixel
is a different distance at 720p and 4K; a pixel-per-frame is a different *speed*
at 30 and 240 fps. Scaling by frame width alone made high-frame-rate 4K worse,
because px/frame falls with the rate while the threshold rose with the width.

Re-expressed against something physical: `view.min_ball_step_px` converts a real
m/s through the scale and frame rate (with slow-motion headroom until the
timebase is settled, exact after — `rate_is_certain`); distances scale off
`body_pixel_height`, `_cricket_r(h, w)` or the track's own measured ball radius
(`track._track_ball_r`); durations are seconds × fps, never frame counts. The
rightward-throw default was removed — without a measurable direction the
downrange filter is skipped rather than assuming a camera side. Also fixed:
per-frame velocity gates that carried no frame rate (wrist teleport, hip rotation
in deg/frame), the release search window clamped to 8–20 absolute frames, and
foot-plant descent as a fixed 6 px.

## Verified

| Clip | Rate | Ball | Speed | Arm | Ball ÷ arm |
|------|------|------|-------|-----|-----------|
| `vid_d6fdd6b445f5.mov` | 30 → **120 measured** | 30 pts | 85.0 | 68.9 | 1.23 |
| `vid_4fcedc79800c.mov` | 120 confirmed | 37 pts | 84.6 | 52.6 | 1.61 |
| `vid_6647732f9011.mp4` | 120 confirmed | 12 pts | 73.2 | 48.5 | 1.51 |
| `vid_dcdb41c98333.mp4` | 203 confirmed | 7 pts | 85.6 | 58.5 | 1.46 |

Ball speed exceeds hand speed on every clip that reports both, and the reference
clip no longer flips between 90 and 120 fps.

## Honest limits

- Reported ball speed is still the component **across the image**. A single
  camera cannot see motion along its own axis, so on anything but a square-on
  view the true release speed is higher; the recession factor is reported so the
  gap is visible rather than hidden.
- Gravity-based rate recovery needs a tracked flight. A slow-motion clip with no
  visible ball still runs on the container rate, and says so.
- 2D hip/trunk proxies remain unreliable on open-chested actions and are refused
  more often at high frame rates, where angular velocities scale up.
