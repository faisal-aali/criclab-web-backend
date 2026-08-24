# TASK-004 — SpinLab-parity pose pipeline, overlay video, Cloudinary, PDF

**Feature:** FEAT-004 → FEAT-012 (measurement-engine rebuild)
**Status:** Done
**Priority:** P0

> Superseded in part by **TASK-006**: release is leave-hand (not peak wrist
> speed — that's MER/cocking), and overlay slow-mo cadence is fps-normalized,
> not a fixed frame-repeat. This file is kept as the original build record.

## Why

The ball-tracking metrics were wrong on real footage (e.g. 1814 km/h) and there
was no processed video. Goal: match SpinLab AI's experience for cricket — a
slowed video with overlaid analysis + a biomechanics PDF — with trustworthy
numbers.

## What changed

- **Measurement engine switched to MediaPipe pose** (`pipeline/pose.py`). Ball
  tracking demoted to best-effort overlay only.
- **Release = peak bowling-wrist speed**; throwing side + action phases in
  `pipeline/action.py`.
- **Scale from pose body height** (`pipeline/calibrate.py`), no fixed cm/px.
- **Biomechanics metrics** (`pipeline/metrics.py`): arm/hand speed, release
  height/angle/time, joint angles per phase, stride %, hip/trunk rotation
  proxies, heuristic action scores. Angular velocities are unwrapped + smoothed
  + robust-percentile + clamped so high-fps jitter no longer explodes; implausible
  speeds are flagged, not reported.
- **Slow-motion overlay video** (`pipeline/render.py`): skeleton (bowling arm
  highlighted), release marker, phase banner, "SLOW MOTION" tag, live metrics
  panel. avc1/H.264 encode.
- **Cloudinary** (`services/cloudinary_service.py`): uploads processed video +
  PDF, returns shareable URLs; local-disk fallback. Creds in `.env`.
- **6-page SpinLab-style PDF** (`pdf/report.py` + `pdf/charts.py`): metric tiles
  with reference bands, kinematic sequence, score rings, angle/hand-speed charts,
  per-phase angle table, AI coach notes + release still.
- **Frontend**: results page shows Cloudinary video + copyable URL, new metric
  cards + score rings; processing stages updated; pinched hero/logo type fixed.
- **Runtime**: backend now runs on **`.venv312`** (Python 3.12) because
  MediaPipe needs numpy<2 and Python ≤3.12.

## Acceptance criteria

- [x] Pose detected across the clip; release matches the hand-speed peak
- [x] Metrics are physically plausible (arm speed ~59 km/h on the test clip)
- [x] Slow-mo overlay MP4 renders with skeleton + release + metrics
- [x] Processed video + PDF upload to Cloudinary and return URLs
- [x] 6-page PDF builds with charts, tables and AI notes
- [x] No LLM-based frame measurement; estimates labelled + confidence carried

## Follow-ups

- **TASK-005** — SpinLab processed-video HUD (grayscale, 2×2 tiles, timeline) +
  metric audit + in-air ball-speed lock (supersedes “ball overlay-only”)
- Multi-view / calibrated capture for true (not 2D-proxy) hip/trunk rotation
- Radar validation to tighten confidence (FEAT-016)
- `nomic-embed-text` coaching memory search (FEAT-015)
- Async Cloudinary upload + progress if large videos slow the job
