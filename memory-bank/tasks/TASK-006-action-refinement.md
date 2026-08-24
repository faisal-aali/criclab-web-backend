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

## Review pass (24 Aug 2026) — 10 findings, all addressed

Multi-angle review of the branch (line-by-line, removed-behaviour, cross-file,
reuse, simplification, efficiency, altitude) surfaced 10 findings; fixes:

- **Stride-window fallback (correctness).** `_peak_in_window` silently widened
  to the whole series when the window held <3 samples, so a "stride peak" could
  come from the follow-through. Now returns `None` + an honest note when a
  requested window is too thin, and the window is bounded **at** release (was
  REL+0.05 s) — a hip peak after REL put sequence item 2 after item 4.
  Confirmed live: hip now lands at 320 vs MER 323 vs REL 327.
- **Spike-proof event frame.** Value was p95 but the exported frame was the raw
  argmax, so a spike the percentile suppressed still became the event frame /
  overlay freeze. Frame now comes from a median-of-3 of the magnitudes.
- **Fabricated gap velocities.** `_angular_velocity` differentiated across runs
  dropped by the degenerate-segment filter, inventing in-band values from an
  arbitrary unwrap branch. Samples spanning >3× the median frame gap are now
  skipped. This alone recovered the 120 fps trunk proxy: **182 deg/s (ok)**,
  previously rejected at a fabricated 2024 deg/s, and sequencing now passes.
- **Falsy-zero time base.** `(base_t or fr)` collapsed every `t_ms` to 0 when
  FFC was frame 0. Now uses the `_t_ms` helper (single definition) everywhere.
- **Series plausibility.** `rotation_series` samples outside the same bands that
  gate the headline metrics become `None` (chart shows a gap via NaN) instead of
  plotting a 3000 deg/s projection artifact as signal — one rule, one meaning.
- **Resolution-dependent guard.** The hip/shoulder foreshortening test was a
  hardcoded 12 px; now `max(8, 0.22 × torso length)`, so it holds at any framing.
- **PDF truth.** Reason notes were pre-truncated to 39 chars upstream of the new
  3-line wrap (cap raised to 84, wrap via `textwrap`); the separation tile pins
  by |value| so a good negative separation no longer sits in the red zone while
  the score ring shows green; `ffc_to_mer` gained the `arm_horizontal` fallback
  `mer_to_rel` already had, so the two p6 rows agree on whether MER was seen.
- **Mongo/API weight.** `rotation_series` is stripped before persisting — it only
  feeds the PDF chart, and the history listing returns full metrics per row.
- **Render decode cost.** Skipped frames are now `grab()`-ed rather than fully
  decoded and colour-converted (~85% of frames on a 203 fps source).
- Dead `win_frames` list removed; pose flicker-filter window widened to 9 with
  its sustained-swap limitation documented (geometry alone cannot catch a swap
  that dominates the window — appearance cues would be needed).

Final state across fps paths: 203 fps ball 86.3 km/h, hip→MER→REL ordered;
120 fps ball 78.4, hip 121 → trunk 182, sequencing True, gap 83 ms;
30 fps hip 1082 → trunk 655, sequencing 100, ball honestly unavailable.

## Branch status

Committed on `refine/action-spinlab-parity` (`e883598` refine, `c09e117`
review fixes) — 2 commits ahead of `main`, clean tree. **Not pushed**:
`git push` returned 403 — the authenticated `gh` account has `pull:true,
push:false` on `Techlio-Pvt-Ltd/criclab-web-backend`. Needs either write
access granted to that account, or a fork + cross-fork PR (not done —
awaiting the user's choice). Push once access exists:

```bash
git push -u origin refine/action-spinlab-parity
```
