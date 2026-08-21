# TASK-006 — Action-mode refinement (metrics honesty, SpinLab cadence, PDF parity)

**Feature:** FEAT-007 (metrics), FEAT-008 (overlay), FEAT-012 (PDF)
**Status:** Done
**Priority:** P0
**Date:** 21 Aug 2026

## Why

Full pass against the SpinLab reference job (`6a68af3aa9a9306b6924893f`, overlay +
6-page PDF, measured frame-by-frame). Numbers/events were already truth-first;
the gaps were metric-definition bugs, overlay cadence, and PDF chart parity.

## What changed

- **Pose** (`pipeline/pose.py`): identity-flicker guard — frames where the
  mid-hip anchor jumps >8% of the frame diagonal from the local 7-frame median
  are dropped (bystanders in frame can steal the landmark lock). Frames are
  dropped, never smoothed/moved. `dropped_outlier_frames` reported.
- **Ball track** (`pipeline/track.py`): downrange-edge blob guard is now
  direction-aware (was hardcoded right-edge, assumed rightward throws).
- **Metrics** (`pipeline/metrics.py`):
  - Hip/trunk rotation peaks are searched over the **delivery stride window**
    (BFC−0.1s → REL+0.05s), not ±80 ms of release — the kinematic chain peaks
    hip → trunk → arm *before* release. Peak **frames** exported
    (`rotation_peaks`), `hip_rotation` phase snaps to the measured hip peak,
    and sequencing is judged on those same frames (was inconsistent argmax).
  - New `hip_to_trunk_peak_gap_ms` metric (SpinLab p6 parity).
  - **Degenerate-projection guards**: line-angle series drop frames where the
    segment's projected length collapses (<30% of its p90) — this is what made
    the trunk proxy read 1700+ deg/s on side-on clips. Hip–shoulder separation
    beyond ±75° is projection collapse → `unavailable` + reason (it previously
    reported −115° and scored it 100/100).
  - `angle_series` smoothed ~30 ms per contiguous run (None gaps preserved);
    `rotation_series` + `event_t_ms` exported for the PDF sequencing chart.
  - Chart series span BFC→FT (+0.25 s pad), not just the wrist-speed window.
- **Overlay** (`pipeline/render.py`):
  - **Playback normalisation**: output is ~real-time outside the delivery
    window and 2× slow-mo inside, regardless of source fps. A 203 fps clip
    previously rendered ~7× slow everywhere (31 s video); now ~15 s with
    exact ~2.5 s event freezes, matching the SpinLab reference cadence.
  - Bottom-left progress % + full-clip grey track with coloured delivery
    segments (SpinLab layout); event tags nudge apart instead of overlapping.
  - Tiles: big value + small unit typography.
- **PDF** (`pdf/report.py`, `pdf/charts.py`):
  - Tiles use red/yellow/green zoned reference bands with a pin (centred good
    zone for stride/release-angle); unavailable reasons word-wrap.
  - New **kinematics sequencing chart** on page 2 (hip/trunk/arm ω + FFC/MER/REL
    event lines) from `rotation_series`.
  - FFC/MER/REL event lines on all angle/velocity charts; wrist-speed chart
    x-axis in ms from FFC; p6 adds "Time between peak hip / trunk (ms)".

## Verified (offline harness: extract→pose→…→pdf, no Mongo/Ollama)

- 203 fps clip: ball 86.3 km/h (in-air lock, REL snapped to leave-hand), arm
  58.4, release 305 ms / 1.60 m; overlay 15.2 s (was 31.7 s); sep −115° now
  honest `—`; trunk 1627 deg/s rejected not clamped.
- 120 fps clip: ball 78.4 km/h, overlay 16.9 s.
- 30 fps clip: hip 1082 / trunk 872 deg/s pass bands, sequencing computes;
  ball honestly unavailable (too few in-air points at 30 fps).

## Notes / limits

- On true side-on footage the trunk-line proxy often exceeds the plausible band
  (projection acceleration through the camera plane) → honest `—`. True trunk
  rotation needs multi-view (FEAT-016).
- API contract unchanged; only additive metric keys. No frontend change needed.
