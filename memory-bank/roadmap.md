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
| FEAT-009 | **S3 + CloudFront hosting** | Done | Presigned PUT originals; worker overlay/PDF; signed GET playback |
| FEAT-010 | Results dashboard | Done | Overlay video + URL, metric cards, score rings, AI sections |
| FEAT-011 | AI agent (Gemma) | Done | Coaching from metrics JSON; catalog-only drill IDs via `gemma3:4b` |
| FEAT-012 | **SpinLab-style PDF** | Done | Event stills + tiles, sequencing, charts, tables, AI notes + drill URLs |
| FEAT-013 | History & compare | Done (basic) | History list + compare delta in agent report |
| FEAT-014 | Ball tracking (Action) | Done | In-air flight lock; headline ball speed when the path leaves the hand |
| FEAT-014b | **Ball flight (stumps)** | Done | Behind-bowler + both wickets; pitch-plane speed/line/length UI |
| FEAT-015 | Coaching memory | In progress (deterministic MVP) | Per-user Action trend stats + AI training plan from `training/{profile,plan}`. Semantic embeddings deferred. |
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
| FEAT-031 | **Daily video quota** | Done | 60 starts/UTC day; FIFO overflow; `expected_start_at`; analysis notify |
| FEAT-032 | **Glacier originals** | Done | Archive `original/` to Glacier Flexible Retrieval when the job is finished for good |
| FEAT-033 | **Honor in-flight cancel** | Done | Cancel stays `cancelled`; worker stops at next stage; quota slot stays used |
| FEAT-034 | **Action clip gates** | Done | `POST /videos` mp4/mov + 100 MiB; fps/duration/1080p on the worker |

## Change log

- **4 Sep 2026 (FEAT-034):** Action `POST /videos` allows only `.mp4`/`.mov` and ≤100 MiB. fps/1080p/10 s stay on the worker. Ball flight suffixes unchanged.
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
- **27 Aug 2026 (TASK-011):** Support ticketing, coaching bookings and the RAG
  assistant — the three modules TASK-010 left unbuilt. Support: threaded
  tickets with attachments, status derived from who spoke last, staff queue,
  readable references (`CL-XXXXXX`). Coaching: availability computed on demand
  from weekly windows rather than materialised, double-booking prevented by the
  existing unique `(coach, slot)` index rather than a check-then-insert, cancel
  and reschedule with a 12h window. Assistant: retrieval-augmented, grounded
  only in `app/assistant/knowledge/*.md`, refuses questions about CricLab's own
  implementation before they reach the model, and strips the answer if a
  disallowed term leaks through anyway. See TASK-011 for the two real bugs the
  build caught.
- **27 Aug 2026 (TASK-012, in progress):** Video-analysis ETA, chatbot overhaul,
  and an admin panel. **Done and verified:** (1) `app/pipeline/eta.py` blends
  each job's own elapsed-time/progress pace with a historical average of the
  last 20 completed jobs for that pipeline, wired into both `/jobs/{id}`
  endpoints as `eta_seconds`. (2) Chatbot: `app/assistant/intent.py` fast-paths
  greetings/thanks/farewells before RAG runs at all; `/assistant/ask/stream`
  streams NDJSON deltas via a new `generate_text_stream()` in
  `ollama_agent.py`; the system prompt now requires Markdown formatting and
  grounds navigation links in a fixed `PAGE_LINKS` table (never an invented
  path); a chain of post-generation repairs
  (`_sanitize_links`/`_humanize_link_text`/`_repair_bare_links`/
  `_collapse_duplicate_href`) fixes the specific malformed-link patterns a 4B
  local model actually produces — each one found by testing, not anticipated
  up front, see TASK-012 for the four distinct bugs and fixes. (3) **A
  foundational gap closed**: video/ball-flight analyses had no `user_id` at
  all — `client.ts` sent no auth header and the upload endpoints never
  recorded who uploaded. Now `VerifiedUser`-gated, `user_id` stored on
  video/job/delivery docs, and every read endpoint scopes to the owner
  (404, not 403, on a mismatch — no signal that the id exists). (4) Admin
  backend: `app/api/admin.py` + `app/services/admin_service.py` — dashboard
  metrics with date-range filtering, paginated user search with
  activate/deactivate, a combined cross-pipeline analyses list, an all-users
  bookings list, ticket metrics, and a notification broadcast with history —
  every route behind `AdminUser`, confirmed 403/401 for non-admin/anonymous.
  Admin frontend is now wired: six pages under `/admin/*` (`RequireAdmin` +
  `AdminLayout`), AccountMenu entry for `role === 'admin'`. Coach CRUD UI and
  booking calendar remain deferred — see TASK-012.
- **27 Aug 2026 (TASK-013):** Ball-speed accuracy and overlay legibility.
  Reported as "shows 100 km/h, should be ~131". Investigated against the real
  4K/120 clip rather than tuning the display: **neither number was right — the
  true image-plane release speed is 86 km/h**, and both wrong numbers came from
  the same root cause. The ball's vertical pixel track is noise at 4K (the
  detector's centroid slides along a 100+ px motion-blur streak), `hypot(dx,dy)`
  cannot distinguish that from real motion, and every frame reporting 130+ km/h
  was a y-spike — including the 132 the overlay was printing beside the ball
  while the panel said 100. Replaced the old two-estimator agreement check
  (which could not work: both estimators consumed the same corrupted signal, so
  agreeing meant nothing) with one physics-constrained robust fit — gravity
  pinned to `g/(mpp·fps²)`, Theil–Sen slopes, interpolated points excluded, and
  a quality gate that refuses rather than guesses. The gate immediately caught a
  second delivery that had been confidently displaying 63.24 km/h off a track
  whose fitted horizontal velocity was *negative*. Overlays are now alpha-blended
  and roughly half their previous width, and the per-frame ball label was
  replaced with the one measured release speed so a single frame can no longer
  contradict itself. Full reproducibility trail logged under `criclab.metrics`.
  **Not done:** the follow-up request for 120→30 fps slow-motion playback,
  auto-generated breakpoints with frame/timestamp/confidence, and player speed
  controls — see TASK-013 for what that needs.
- **2 Sep 2026 (TASK-014 / FEAT-031):** Daily video quota. The website API
  schedules queued Action and Ball-flight jobs onto UTC days (default 60
  starts/day), writes `expected_start_at`, and notifies when that date moves.
  The video worker is the only process that increments `quota_days.started`.
- **2 Sep 2026 (FEAT-031 worker cost):** This always-on API starts the separate
  worker EC2 only when a queued clip can begin now. A lifespan loop (not cron)
  recomputes at boot and at 00:00 UTC so leftover FIFO jobs start after the cap
  resets. Wake retries while the instance is still stopping.
- **3 Sep 2026 (TASK-015 / FEAT-032):** Finished originals move to Glacier Flexible
  Retrieval immediately (`completed`, `failed`, queued-`cancelled`). Age-based
  lifecycle is forbidden — quota overflow still needs GetObject. In-flight cancel
  is honored by the worker at the next stage, then archived. Stale `claimed` is
  re-queued; stale `processing`/`analyzing` fails and archives. Reused Glacier
  keys are rejected on POST.
- **3 Sep 2026 (FEAT-033):** Cancel on a running job stays `cancelled`. Progress
  writes cannot overwrite it. The worker finishes the current stage, skips the
  rest, and does not persist a delivery. The daily slot stays used.
