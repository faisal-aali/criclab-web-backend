# TASK-005 — processed-video HUD + metric audit

**Feature:** FEAT-008 (overlay HUD), FEAT-007 (metric formulas), FEAT-014 (in-air ball speed)
**Status:** Done
**Priority:** P0
**Date:** 13 Aug 2026

## Why

The results page already showed metrics, but (1) ball speed locked onto the
bowling **hand** (~6.3 km/h) even when the ball was visible in the sky, and
(2) the processed MP4 did not match SpinLab’s grayscale clip (2×2 tiles,
kinematic sequence, color-coded trail, bottom event timeline).

## Where we were (13 Aug 2026, before this task)

- Required player profile (height ft/in, bowling arm, weight lbs, DOB, style)
  before Analyze — `pipeline/profile.py` + UploadPage + `POST /videos`.
- Ball tracker rewritten: dark-on-sky ovals + red/white colour + motion;
  seed on the object that **left the hand**; reject wrist/tree locks.
- On `storage/videos/vid_132e9d742cb1.mp4` (1920×1080, ~203 fps): in-air lock
  ~**61 km/h** (was 6.3 km/h on the wrist). Clip is a dark oval in flight
  (American-football-shaped), not a small red cricket ball — detector must
  keep large ovals.
- Overlay was still color footage + bottom-left HUD + small FFC/ARM/REL/FT chips.
- FEAT-014 in the roadmap still said “overlay-only / MOG2+RANSAC” — stale.

## Metric formulas (truth contract)

CV + physics only. Gemma never invents numbers. Same JSON for UI, PDF, overlay.
Reject — never clamp a bad value into a “nice” number.

| Metric | Formula | Gate |
|--------|---------|------|
| Ball speed | ballistic fit on detected in-air points (blend with robust per-frame median when they agree) × height scale | 25–160 km/h; path must leave the hand |
| Arm speed | peak bowling-wrist px/frame × mpp × fps | 15–160 km/h; arm from **player profile** |
| Release time | (release − FFC) / fps × 1000 ms | FFC plant 120–550 ms before release |
| Release height | (ground_y − wrist_y) × mpp vs planted lead ankle | needs FFC + scale |
| Elbow | interior shoulder–elbow–wrist on most in-plane frame near release | 180° = straight |
| Arm-swing | unwrapped bowling-arm segment ω near release (**deg/s**, not degrees) | smoothed; high-fps jitter rejected |
| Stride | lead–trail ankle px / body height px | needs FFC |
| Hip–shoulder sep | 2D image-plane proxy only | always **estimated** |

## Timeline events (cricket names, omit if not seen)

- **BFC** — trail-ankle plant before FFC
- **FFC** — lead-ankle plant ( FP)
- **MER** — max bowling-arm cocking between FFC and release (min interior elbow / wrist furthest behind the head)
- **REL** — leave-hand (wrist after cocking; snapped to ball path when tracked)
- **FT** — wrist-speed decay after release

Kinematic sequence (4 numbered items):

1. Front-foot contact
2. Hip rotation (peak 2D hip–shoulder change; labeled estimated)
3. Arm horizontal / MER
4. Elbow extension at release

## Overlay spec (`pipeline/render.py`)

- Desaturate the frame (SpinLab grayscale) so color overlays read
- Skeleton: bright joint nodes; bowling arm in CricLab orange
- Trail: thick smoothed bowling-wrist path (blue wind-up → green FFC → yellow MER → purple REL) with event beads; ball path after REL; REL marker on the wrist at leave-hand
- Top bar: `CricLab` + player name + height from profile (never SpinLab / 3motionAI)
- Top-right 2×2 tiles: Ball speed · Arm speed · Release height · Release time (`—` if status ≠ ok)
- Kinematic sequence list with numbered discs that fill when the event is reached
- Bottom timeline: **BFC · FFC · MER · REL · FT** tags, colored bar segments, numbers 1–4, playhead
- Slow-mo: repeat frames in the delivery window; **hold ~2.5 s on BFC / FFC / MER / REL / FT** (SnLab freeze); output 30 fps; encode avc1 → mp4v

## Honest limits

One camera, 2D, image-plane km/h using stated height — not a radar gun.
Side-on with the ball in view after release. Front-on cannot yield true speed.
Old History rows do not update — **re-upload**.

## Acceptance

- [x] Ball speed on the test clip is ~60 km/h band (not 6.3, not 1800) — measured **58.4 km/h** with pose seed, **60.6 km/h** with known wrist
- [x] Processed MP4 is grayscale with 2×2 tiles, sequence list, and timeline
- [x] Unavailable metrics show `—` plus the existing note (arm-swing 5197 deg/s rejected)
- [x] Memory bank (this file + roadmap + systemPatterns + productBrief) matches the code

Verified 13 Aug 2026 on `storage/videos/vid_132e9d742cb1.mp4` (870 frames, ~203 fps, height 1.8 m). Re-upload in the app to refresh History.

## Follow-ups (not this task)

- Radar / multi-view validation (FEAT-016)
- `nomic-embed-text` coaching memory (FEAT-015)
- Async Cloudinary upload if large videos stall the job
