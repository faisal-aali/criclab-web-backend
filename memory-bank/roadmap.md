# Roadmap — Cric-Lab

High-level features. Detail lives in `tasks/`.

| ID | Feature | Status | Summary |
|----|---------|--------|---------|
| FEAT-001 | Project foundation | Done | Vite React FE + FastAPI BE + MongoDB + storage folders |
| FEAT-002 | Video upload & jobs | Done | Upload bowling video, job status API, processing UI |
| FEAT-003 | Video pipeline | Done | OpenCV extract + metadata |
| FEAT-004 | **Pose estimation** | Done | MediaPipe BlazePose 33-landmark track (measurement engine) |
| FEAT-005 | **Action/release detection** | Done | Throwing side (profile arm wins) + leave-hand release + action phases |
| FEAT-006 | Calibration | Done (basic) | Scale from pose body height or provided reference |
| FEAT-007 | **Biomechanics metrics** | Done | Leave-hand arm speed, joint angles, timing, rotation proxies, scores from ok metrics only |
| FEAT-008 | **Slow-mo overlay video** | Done | SpinLab HUD: colour frames + 2×2 tiles + BFC/FFC/MER/REL/FT timeline |
| FEAT-009 | **Cloudinary hosting** | Done | Upload processed video + PDF; return shareable URLs |
| FEAT-010 | Results dashboard | Done | Cloudinary video + URL, metric cards, score rings, AI sections |
| FEAT-011 | AI agent (Gemma) | Done | Coaching from metrics JSON; catalog-only drill IDs via `gemma3:4b` |
| FEAT-012 | **SpinLab-style PDF** | Done | Event stills + tiles, sequencing, charts, tables, AI notes + drill URLs |
| FEAT-013 | History & compare | Done (basic) | History list + compare delta in agent report |
| FEAT-014 | Ball tracking (Action) | Done | In-air flight lock; headline ball speed when the path leaves the hand |
| FEAT-014b | **Ball flight (stumps)** | Done | Behind-bowler + both wickets; pitch-plane speed/line/length UI |
| FEAT-015 | Coaching memory | Planned | Embeddings via `nomic-embed-text` + semantic search |
| FEAT-016 | Validation | Planned | Radar / ground-truth checks; multi-view for true rotation speed + the depth component of ball speed |
| FEAT-018 | **Capture-rate recovery** | Done | Slow-motion clips timed from the ball's fall, not the container fps |
| FEAT-019 | **Delivery type & throwing screen** | Done | Pace band from measured speed; ICC 15° screening only where the view supports it |
| FEAT-017 | Train / drills | Done | Closed YouTube catalog + DrillShelf + `/train` library |
| FEAT-020 | **Accounts & auth** | Done | JWT access + rotating refresh, OTP email verification, recovery, guards |
| FEAT-021 | **Transactional email** | Done | SMTP over aiosmtplib, branded templates, credentials env-only |
| FEAT-022 | **Notifications** | Done | In-app centre, unread badge, keyset paging |
| FEAT-023 | Support ticketing | Planned | Collections + indexes ready; no service or UI yet |
| FEAT-024 | Coaching bookings | Planned | Collections + indexes ready; no service or UI yet |
| FEAT-025 | RAG assistant | Planned | `kb_chunks` indexed; no retrieval or UI yet |

## Change log

- Initial greenfield build under `CricLabMLReview` with working upload → analysis → PDF path
- Replaced Notera Memory Bank with Cric-Lab product context
- **SpinLab-parity rebuild:** switched the measurement engine from ball tracking
  to MediaPipe **pose**; added slow-motion overlay video, **Cloudinary** upload
  (video + PDF URLs), a 6-page SpinLab-style PDF, and robust/honest biomechanics
  metrics. Backend now runs on the **Python 3.12** venv (`.venv312`).
- Fixed pinched hero/logo typography (relaxed letter-spacing + line-height).
- **18 Aug 2026:** Two film modes (Action vs Ball flight). Gemma coaches and
  picks drills from `app/coaching/drills.json` only. Action arm speed is
  leave-hand, not cocking peak. Missing ball speed no longer scores from arm
  speed. FEAT-016 (radar validation) remains Planned — stump speed is pitch-plane,
  not a gun.
- **21 Aug 2026 (TASK-006):** Action refinement pass against the measured SpinLab
  reference. Hip/trunk peaks over the stride window (peak frames drive the
  sequencing check + hip-rotation event), degenerate-projection guards on
  line proxies and hip–shoulder separation, pose identity-flicker filter,
  overlay playback normalised (real-time outside window, 2× inside, ~2.5 s
  freezes, progress %), PDF gains zoned band tiles + kinematics sequencing
  chart + event lines on all charts. New `hip_to_trunk_peak_gap_ms`.
- **24 Aug 2026 (TASK-007):** Slow-motion clips are now timed correctly. A 30 fps
  container holding a 120 fps capture made every speed read 4x slow and cut every
  phase window 4x too narrow; `pipeline/timebase.py` recovers the real rate from
  the ball's own fall under gravity and the action pass is redone on it. Release
  is taken from the frame the ball leaves the hand and now drives FFC/BFC/MER
  detection; foot contact is found as the start of the ankle's final plateau
  rather than the hardest strike in the window. Ball tracking gained a second
  (RANSAC) path generator, per-candidate optical-flow validation, residual-based
  outlier rejection, and a quadratic image-x model — a receding ball's pixel
  speed decays, and assuming it constant was discarding the frames nearest
  release. Ball speed is now read at release and always exceeds arm speed. New
  `delivery_type`, `action_legality` (conservatively gated), `speed_consistency`.
- **24 Aug 2026 (audit):** Results UI now renders pace band, ICC 15° screening,
  capture rate, and ball-vs-arm consistency. Compare is same-player ball speed
  only (arm is a separate field). Ball-flight length rejects off-pitch bounces
  instead of clipping them onto the square.
- **26 Aug 2026 (TASK-008):** Generalisation pass so any upload analyses, not just
  the clips it was tuned on. Ball tracking and ball *speed* were separated: a
  validated flight is now kept and drawn even when its geometry cannot support a
  km/h (previously the whole path was deleted, so clips shot from behind or down
  the pitch showed no ball at all). Roughly twenty thresholds that were absolute
  pixels or absolute pixels-per-frame — speed floors, seed radii, wrist-teleport
  limits, foot-plant descent, hip-rotation rate, candidate size quotas, release
  and back-projection windows — were re-expressed against body pixel height, the
  ball's own measured radius, or a real m/s converted through the scale and frame
  rate. The rightward-throw default was removed. Capture-rate recovery now covers
  phone super-slow-motion (up to 32x, 960 fps).
- **26 Aug 2026 (TASK-009):** Finding the ball and quoting its speed were split
  apart, so clips filmed from behind or down the pitch now track and draw the
  ball instead of showing none. Pose informs the ball search — the bowler's own
  limbs are excluded as candidates. Capture-rate recovery stopped flipping
  between adjacent multiples by measuring the ball's recession from its apparent
  size rather than reading it off a noisy cubic term, and now covers phone
  super-slow-motion up to 32x. Added cross-validation between independently
  measured quantities (pose timing, ball timing, hand speed vs arm-swing rate,
  release height vs stature); failing checks lower confidence and are reported.
  Roughly twenty absolute-pixel / pixel-per-frame thresholds were re-expressed
  against body size, ball size or a real m/s.
- **26 Aug 2026 (TASK-010):** Accounts platform. CricLab had no authentication at
  all; it now has signup with emailed OTP verification, sign-in, short-lived JWT
  access tokens with rotating opaque refresh tokens, password recovery and
  change, session listing and revocation, route guards, Mongo-backed rate
  limiting, a security audit trail, branded transactional email over SMTP, and an
  in-app notification centre. Three real bugs were caught by the end-to-end suite
  — see TASK-010. Support, coaching and the assistant are not built.
