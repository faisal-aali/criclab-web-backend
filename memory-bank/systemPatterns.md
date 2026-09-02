# System Patterns — Cric-Lab

> **Most important Memory Bank file.** Read before writing code. These rules override vague prompts.

## Architecture rule #1

**Computer vision + physics produce measurements. The LLM coaches; it never measures.**

Never ask `gemma3:4b` to estimate speed/angles from raw video frames (that *increases* false km/h). There is no custom cricket LLM. Gemma runs *after* CV and only narrates `status === ok` JSON, then picks **catalog drill IDs**. Pipeline order (Action mode):

```text
Upload (this API) → queued Mongo job
  → criclab-video-service worker: POSE → action → calibrate → track → metrics
    → overlay → Cloudinary → Gemma narrative + drill matching → PDF
      → persist delivery → React polls this API
```

## Three processes (do not treat `app` as one package)

| Process | Repo | Owns |
|---------|------|------|
| Website API | **this repo** | Auth, upload, insert `queued` jobs, job/delivery **reads**, stump *still* calibration, signed Cloudinary upload, chat assistant, Train catalog HTTP, bookings |
| Video worker | `criclab-video-service` | Everything after claim: MediaPipe/OpenCV, metrics, overlay, PDF, Gemma *video* notes, drill matching, overlay/PDF upload, delivery **writes** |
| UI | `criclab-web-frontend` | Display API JSON only |

`app.coaching` in **this** repo is catalog I/O (`load_catalog` / `save_catalog` for Train and `/admin/drills`). `app.coaching` in the video service is matching (`weakness_tags`, `balltrack_tags`, hydrate). They are not the same package. Admin edits to `drills.json` here do not auto-sync to the worker snapshot.

Same relative filenames (`agent/ollama_agent.py`, `balltrack/stumps.py`) are allowed when the role differs. Do not import video-service modules from this API, or vice versa.

## Two film modes (do not merge their numbers)

| Mode | Route | Camera | Truth |
|------|--------|--------|--------|
| **Action** | `/` | Side-on full-body | Mechanics: sequence, brace, stride, elbow, leave-hand arm speed. Ball km/h is an **image-plane + height-scale estimate**, or `—`. |
| **Ball flight** | `/ball-flight` | ~4 m behind non-striker, **both stump sets** | Pitch-plane speed / line / length via stump homography (`app/balltrack/`). Validate ~45–155 km/h. Not a radar gun. |

Wrong camera on Action still shows **—** for km/h. Never paste stump speed onto a pose job as a fake headline.

## Architecture rule #2 — pose is the measurement engine; ball speed needs a real lock

Pose (MediaPipe) drives release, FFC, joint angles, stride, arm-swing. **Ball
speed is the headline metric only when the ball is tracked in flight** — a path
that leaves the bowling hand and keeps moving downrange. A lock on the wrist,
torso, fence, or a stationary tree is not a ball track: return null + reason.

- **Release = leave-hand**, not the highest wrist (cocking / MER). Pose: last
  near-peak bowling-wrist speed after the highest hand, along the throw.
  If the ball is tracked, snap to the last frame the wrist is still on the
  back-projected in-air path. REL marker stays on the bowling wrist.
- **Front-foot contact** = lead-ankle plant **60–600 ms** before release (omitted if not found).
- **Back-foot contact** = trail-ankle plant before FFC (omitted if not found).
- **MER** = max bowling-arm cocking between FFC and release (omitted if not found).
- **Arm speed** = bowling-wrist px/frame **at leave-hand** (not cocking / gather peak) × scale × fps.
- **Scale** from upright (90th-pct) head→ankle × **user-provided** height or mpp.
  Never invent 1.7 m. Without user scale → physical km/h/m are unavailable + note.
- **Ball speed (Action)** = one physics-constrained robust fit on the detected
  in-air path, × height scale (`track.robust_release_velocity_px_per_frame`):
  gravity pinned to `g/(mpp·fps²)` so y-noise cannot become speed, Theil–Sen
  slopes so a streak-centroid outlier cannot bend the line, interpolated points
  excluded. Refused when the fit's own residuals score below
  `MIN_BALL_FIT_QUALITY`, or view/geometry fail. Sanity band ~25–160 km/h.
  Reject — never clamp. (This replaced a two-estimator agreement check that
  could not work: both estimators consumed the same corrupted `hypot(dx, dy)`,
  so they agreed on a number ~16% too high. See "Ball speed — the y-axis is
  noise" below.)
- **Ball speed (Ball flight)** = stump-homography pitch-plane distance/time. Separate job.
- **Truth contract**: measure or null+reason. Sanity gates reject — never clamp a
  bad number into a “nice” value. UI/PDF/overlay share the same metrics JSON (`status === ok`).
- **2D hip/trunk rotation** is advanced proxy only — not headline truth. Peaks are
  measured over the delivery-stride window (hip → trunk → arm peak *before*
  release), and the same peak frames drive the sequencing check and the
  hip-rotation event. Reject values outside a cricket-plausible deg/s band
  (do not clamp spikes). Line angles from a foreshortened segment (shoulders
  pointing at the camera) are projection noise — drop those frames; a
  hip–shoulder separation beyond ±75° is projection collapse → unavailable.
- **The timebase is a measurement, not metadata.** A slow-motion export carries
  its *playback* rate in the container (30 fps) while each frame holds 1/120 s of
  real time. Every km/h and every millisecond then reads several times too slow,
  and every phase window ("front foot plants 60-600 ms before release") is cut
  that many times too narrow in frames. `pipeline/timebase.py` solves the ball's
  fall (`a_px = g / (mpp · fps²)`) for the real rate, and the action pass is redone
  on it. Candidates are whole multiples of the container rate. Two curvature
  readings are reconciled by their known bias directions: whole-arc curvature is
  biased low (fps biased high) because the ball recedes; the cubic's quadratic
  term at release is unbiased but noisier — the first bounds, the second centres.
  Never override without a tracked flight; say the rate came from the file instead.
- **A receding ball's image-x is not linear.** Pixels-per-frame decay through the
  flight as the delivery goes downrange. Fit x(t) as a quadratic wherever the path
  is modelled; a linear fit mis-predicts the frames nearest release by tens of
  pixels, which both under-reads release speed and makes outlier filters discard
  the early flight. Read release speed as the derivative at the *first* in-air
  sample, not a median over the arc.
- **Release comes from the ball when there is one.** `ball_leave_frame()` gives the
  last frame the wrist is still on the ball; `analyze_action(release_override=...)`
  then searches every other event relative to that measured release. Wrist-speed
  refinement is the fallback, not the primary.
- **Foot contact = the start of the ankle's final plateau**, not the hardest
  downward strike in the window. At high frame rates the window spans run-up
  strides that strike harder than the delivery stride.
- **Ball speed must exceed arm speed.** The ball leaves from beyond the wrist on
  the same rotating arm. If it comes out lower, the ball is foreshortened — report
  it as a lower bound with the reason (`speed_consistency`), never silently.
  Reported ball speed is the component *across the image*; a single camera cannot
  see motion along its own axis.
- **A validated in-air flight outranks the pose-based view guess** for speed. The
  shoulder-width classifier reads a mixed/open action as front-on and would refuse
  every speed on a clip whose ball plainly crosses the frame. Pose-only clips still
  defer to the classifier.
- **Release height is not gated on a side-on view** — it is a vertical distance and
  camera yaw does not foreshorten vertical pixels. Stride length still is.
- **Throwing (ICC 15°) screening is refused unless the view earns it.** From one
  camera an arm angled at the lens projects a straight elbow as bent by more than
  the 15° the whole test turns on. Report elbow angle at release always; report an
  extension and a verdict only on a genuinely side-on swing that passes through
  upper-arm-horizontal, and word it as screening — never as a call. A false
  "illegal action" is a far worse failure than an honest `—`.
- **Finding the ball and quoting its speed are separate questions.** A tracked
  flight is kept whenever it is a real moving object that left the hand
  (`view.flight_is_trackable`) — it draws in the overlay, pins release, and feeds
  the gravity timebase. Whether its direction supports an image-plane km/h is a
  *second* gate (`view.flight_geometry_ok`), applied in metrics, which refuses the
  speed with its own reason. Deleting the path because the speed is unmeasurable
  blanks the ball on every clip filmed from behind or down the pitch.
- **No threshold in absolute pixels, and none in pixels-per-frame.** A pixel means
  a different distance at 720p and 4K; a pixel-per-frame means a different *speed*
  at 30 fps and 240 fps. Scale every gate by something physical:
  - distances → `body_pixel_height`, the ball's own measured radius
    (`track._track_ball_r`), `_cricket_r(h, w)`, or the frame diagonal;
  - speeds → convert a real m/s through `view.min_ball_step_px` (mpp × fps);
  - durations → seconds × fps, never a frame count.
  Scaling by frame width alone is not enough — it makes high-frame-rate 4K floors
  *worse*, because pixels-per-frame falls with the rate while the floor rises with
  the width. Before the timebase runs, treat the container rate as a lower bound
  on the capture rate and allow headroom; after it, be exact (`rate_is_certain`).
- **Never assume which way the delivery travels.** Where the throw direction cannot
  be measured from pose, skip the downrange filter rather than defaulting to
  rightward — a default is a camera-setup assumption that discards every real
  candidate on a clip shot from the other side.
- **Pose first, ball second — and the ball search uses the pose.** Pose runs
  before ball tracking so the bowler's own limbs can be excluded as ball
  candidates (`runner._body_segments` → `track.track_ball_from_release`). A
  forearm, shoulder or thigh is a fast, ball-sized, ball-coloured blob and is the
  single largest source of false locks. The bowling forearm is deliberately left
  out of the exclusion map: at release the real ball is right beside it.
- **Cross-validate independently measured quantities, do not just band-check
  each one.** `metrics._cross_validate` compares things measured by different
  routes — pose timing against ball timing, hand speed against the arm's angular
  rate, release height against the bowler's own stature, ball speed against hand
  speed. Each number can sit inside its own band while together describing a
  delivery that could not have happened; a wrong frame rate or a lock on the
  wrong object shows up here as a contradiction. Failing checks lower confidence
  and are reported — nothing is silently corrected.
  The wrist-speed-vs-arm-swing check is the strongest of these: the two sides
  come from different measurement chains, so agreement is real evidence that both
  the capture rate and the pixel scale are right.
- **The ball's apparent size measures its recession.** Radius is inversely
  proportional to distance, so the ratio of ball size at the start of the flight
  to the end is the ratio of those distances. `timebase._depth_growth` uses that
  to de-bias the arc-averaged gravity fit, which recovers the capture rate from
  the *precise* fit instead of the noisy cubic one. Without it the rate could
  flip between two adjacent multiples (90 vs 120 fps) on a small change in the
  tracked points.
- **Profile `bowling_arm`** wins over auto side detection.

## Accounts, tokens and secrets

- **Never confirm whether an address has an account.** Signup, sign-in and
  password recovery answer identically whether or not the email is known. A
  duplicate signup mails the real owner rather than telling the caller.
- **Access tokens are JWTs; refresh tokens are not.** Refresh tokens are opaque
  random strings persisted only as SHA-256 digests, so a database leak yields no
  usable sessions and any one token can be revoked. Rotate on every renewal.
- **Invalidate by marker, not by clock.** Tokens carry `pwd_at`, the account's
  password-change stamp, compared against the current value. Comparing `iat`
  against a timestamp cannot work: JWT issues at whole-second resolution, so a
  replacement token minted in the same second as the change is
  indistinguishable from the one being replaced.
- **The Mongo client is `tz_aware=True`.** BSON stores UTC millis and the driver
  otherwise returns *naive* datetimes — which serialise without an offset, so
  browsers read them as local time, and server-side comparisons against an aware
  "now" are wrong by the machine's UTC offset.
- **Uniqueness belongs to the index.** A read-then-write check in a handler is a
  race two concurrent signups both win.
- **`public_user()` is an allow-list.** New stored fields are not exposed until
  named, so a future secret cannot leak by being forgotten.
- **Sending email never decides request success.** Signup completes with the mail
  server down; the user resends.
- **Secrets live in `.env` only** (gitignored) and in `Settings`. Never logged,
  never returned, never in an exception message. `.env.example` carries
  placeholders. Recipient addresses are masked in logs.
- **Rate limits are Mongo-backed** so they hold across workers, and TTL-indexed
  so counters expire themselves.
- **Guards hide UI; the API enforces access.** Every handler scopes to the caller
  by putting `user_id` in the query filter — never trusting a client-supplied id.
- **A frontend route guard is not the same claim as "every API call from that
  page carries identity."** `/videos` and `/balltrack/sessions` sat behind a
  gated frontend route for a full task (TASK-010/011) while the actual
  request had no Authorization header at all — the old `client.ts` used a
  bare `fetch()`, never `authFetch`. Caught while building the admin panel's
  per-user analysis counts, which could not be real without it. When adding a
  new owned resource, verify the *request*, not just the *route*, carries the
  caller's identity — read the actual fetch call, don't infer it from which
  page uses it.

## Admin panel authorization

- **Every admin route depends on `AdminUser`, with no exceptions and no
  route that "will add it later."** `app/api/admin.py` exists as one file
  specifically so this is auditable at a glance — every handler in it takes
  an `AdminUser`-typed parameter, and nothing outside this file queries across
  every account's data. Confirmed 403 for a signed-in non-admin and 401 for
  anonymous against every route before considering it done (see TASK-012).
- **Sensitive admin actions go through the same audit trail as sensitive user
  actions** (`auth_repo.log_security_event`) — disabling an account and
  sending a broadcast are logged the same way a password change or a
  sign-out-everywhere already were. Not a parallel logging system.
- **An admin cannot disable their own account** (checked before the write,
  not left to "well, they probably won't") — the one guard specific to this
  panel that isn't just "requires AdminUser."

## Support, coaching and the assistant

- **Ownership is a query filter, not a check.** Every user-facing ticket and
  booking handler puts `user_id` in the Mongo filter itself; a valid id
  belonging to someone else returns 404, the same answer as a bad id — never a
  403 that would confirm the id exists.
- **Double-booking is prevented by the unique index, not by application logic.**
  `available_slots()` is computed fresh from weekly availability windows minus
  live bookings; it is a read, not a reservation. The unique partial index on
  `(coach_id, starts_at)` filtered to `pending|confirmed` is what actually stops
  two people taking the same slot — `create_booking` inserts and lets Mongo
  reject the loser, rather than checking-then-inserting and racing.
- **Reschedule books the new slot before releasing the old one.** If the new
  insert fails (someone else took it), the original booking is untouched. The
  reverse order would lose the slot the person already had.
- **Ticket status is derived from who spoke last**, not set by hand: a user
  reply moves it to `awaiting_support`, a staff reply to `awaiting_user`.
  Nothing needs to remember to update it, so it cannot drift from the thread.
- **Uploaded attachments are trusted by neither name nor extension.** The
  filename is a display label only; the file is written to a server-chosen path
  under a server-chosen id, and its type is decided by the declared
  content-type against an allow-list, not by trusting either the name or the
  claimed type alone. Served back with `X-Content-Type-Options: nosniff` so a
  disguised upload cannot be rendered inline as HTML.
- **The assistant is grounded, not instructed to be honest.** Three independent
  layers, not one: (1) retrieval hands it only passages from
  `app/assistant/knowledge/*.md` — there is no internal documentation in that
  corpus for it to leak; (2) a question *about* CricLab's own implementation is
  pattern-matched and refused before it ever reaches the model; (3) the
  generated answer is scanned for a denylist of implementation terms and
  discarded — not trimmed — if one appears, since a model told not to say
  something is not the same as a model that structurally cannot.
- **Retrieval relevance needs two thresholds, not one.** A general-purpose
  embedding model does not put unrelated text near zero cosine similarity — two
  sentences with nothing in common still score around 0.45. Similarity is
  rescaled onto the band that actually carries signal before any cutoff is
  applied, and the *best* passage must additionally clear a higher bar than the
  rest (`MIN_TOP_RELEVANCE`), or the whole result set is discarded. Four weak
  passages are not evidence of relevance; they are four ways to answer the
  wrong question. Caught during build by "what is the airspeed of a swallow?"
  getting back a confident paragraph about ball speed.
- **Retrieval degrades to term overlap, not to an error**, when the local
  embedding model is unreachable — a support assistant that stops answering
  because a model is down is worse than one that answers a little less
  precisely.
- **Casual messages skip retrieval entirely.** `app/assistant/intent.py`
  classifies a short, whole-message greeting/thanks/farewell *before* RAG or
  the LLM are touched, and answers directly. Deliberately narrow (a length
  cap, whole-message match only) so "hi, why wasn't my ball tracked?" still
  gets treated as a real question rather than a greeting with the actual
  question silently dropped.
- **Streaming output still has to pass the same leak-scan and link-allow-list
  as a non-streaming answer** — buffering happens in fixed-size chunks
  (`STREAM_FLUSH_CHARS`) specifically so the leak regex always sees a full
  chunk of context, not a token at a time. A `redacted` event replaces
  whatever the client had already shown if a check fires mid-stream; that is
  the actual security boundary, not the prompt telling the model to behave.
- **A small local model will write a markdown link's *intent* correctly and
  its *syntax* inconsistently** — bare `[Label]` with no href, the raw path
  used as the label, the href duplicated right after itself, or the whole
  thing split across two streamed chunks. Every one of these was found by
  reading actual streamed output, not by inspecting the prompt, and each has
  its own narrow repair function in `app/assistant/chat.py`
  (`_repair_bare_links`, `_humanize_link_text`, `_collapse_duplicate_href`,
  `_split_before_open_bracket`/`_strip_leading_duplicate`) rather than one
  attempt to make the prompt stricter. Treat the model's link as a signal of
  *intent*, not as trustworthy final syntax.
- **A prompt's own example gets copied verbatim.** An early version of the
  link instructions used one real link (`[Open Coaching](/app/coaching)`) as
  the format example; the model then appended that exact link to answers
  about forgotten passwords and untracked balls. Fixed by using a placeholder
  pattern in the instructions instead of a real example, and giving each row
  of the allowed-links table an explicit "for questions about X" hint so the
  model has a reason to pick the *right* link, not just *a* link.
- **ETA blends a job's own pace with recent history, not either alone**
  (`app/pipeline/eta.py`). Elapsed-time-over-progress is nonsense at low
  progress (early stages are quick relative to later ones); a flat historical
  average ignores that *this* video might be longer or the machine busier.
  The blend weight shifts toward "trust this job" as progress advances.

## Frontend API contract (sibling repo: criclab-web-frontend)

UI lives in `../criclab-web-frontend`. Backend must keep this contract stable:

- Job polling via `/jobs/{id}` and `/balltrack/jobs/{id}` with `status`, `progress`, `stage`, `message`
- Metrics JSON: physical values only when `status === 'ok'` (null + reason otherwise); UI gates cards on that
- Relative artifact URLs (`/artifacts/...`, `/media/videos/...`) so the frontend can prefix with its API base
- CORS via `CORS_ORIGINS` must include the Vite origin (and any deployed frontend)
- React never talks to MongoDB or Ollama — only this FastAPI

## Backend (FastAPI) — this repo

- Thin route handlers: validate → insert `queued` job → return job IDs
- React never talks to Mongo, Ollama, or the video worker — only this FastAPI
- `pipeline/` here is ETA, daily quota schedule (`quota.py`), and player-profile parse
- Chat assistant lives in `agent/` + `assistant/`; video coaching notes live in the worker
- Catalog HTTP: `coaching/drills.json` + `recommend.py` load/save only
- **Daily quota:** this API assigns `available_at` / `expected_start_at` and notifies. It never increments `quota_days.started` — only the video worker does that. Cancel is queued-only. `kind=analysis` notifications fire on insert (email if deferred to a later UTC day) and when `scheduled_date` changes.
- **Worker EC2 wake:** this always-on API starts the **separate** worker instance (`WORKER_EC2_INSTANCE_ID`) only when a clip can begin now (`has_claimable_job`: eligible queued + a slot left today). Upload does not poke on insert; `schedule_queued_jobs` may. A lifespan task recomputes at boot and at **00:00 UTC** (not cron/EventBridge). If the instance is still `stopping`, wake retries for ~2 minutes. Locally the poke is a no-op.

## Pipeline modularity

CV stages run in **criclab-video-service**, not this process. When adding a bowling metric, extend the worker metrics stage + UI cards + PDF tiles — do not put pose/PDF back in this API.

| Stage | Where | Module |
|-------|--------|--------|
| Queue insert / job reads | this API | `api/videos.py`, `api/balltrack.py` |
| ETA for the UI | this API | `pipeline/eta.py` |
| Daily quota schedule / notify | this API | `pipeline/quota.py` |
| Worker EC2 start-if-stopped | this API | `services/ec2_worker.py` (claimable + UTC midnight) |
| Stump still boxes | this API | `balltrack/stumps.py` |
| Extract → pose → metrics → overlay → PDF | video service | `pipeline/*`, `pdf/*` |
| Ball flight video | video service | `balltrack/runner.py` |
| Gemma video notes + matching | video service | `agent/ollama_agent.py`, `coaching/recommend.py` |
| Train / admin catalog | this API | `api/coaching.py`, `api/admin.py` |

## Overlay video (SpinLab-parity, CricLab brand)

`render.py` burns analysis onto the **original colour** frames. Grayscale is
only used inside detection (flow, contours) — never written to the overlay.
Layout matches SpinLab’s processed clip, cricket labels only:

- Skeleton: bright joint nodes; bowling arm in CricLab orange
- Trail: thick smoothed bowling-wrist path, phase-colored (blue wind-up →
  green FFC → yellow MER → purple REL/FT) with beads at BFC/FFC/MER/REL/FT;
  ball path continues after REL. REL is the wrist at leave-hand, not the sky.
- Top bar: `CricLab` + player name + stated height (never SpinLab / 3motionAI)
- Top-right 2×2 tiles from the **same metrics JSON**: Ball speed, Arm speed,
  Release height, Release time (`—` if status ≠ ok)
- Kinematic sequence list (1 FFC, 2 hip rotation est., 3 MER / arm horizontal,
  4 elbow extension at release); numbered discs fill when the event is reached
- Bottom timeline: **BFC · FFC · MER · REL · FT** tags, colored bar segments,
  numbers 1–4, playhead. Omit a tag if that phase was not seen.
- **Event pause:** hold each of those frames for ~2.5 s (SpinLab-style freeze),
  with a CricLab event banner. Gentle 2× slow-mo between events in the delivery
  window. Output 30 fps. Encode `avc1` → `mp4v`; Cloudinary `q_auto,vc_h264`.
- **Playback normalisation:** playback is ~real-time outside the delivery
  window and 2× inside it *regardless of source fps* — a 200 fps slow-mo
  capture must not render as a ×7 crawl. Bottom-left progress % over a
  full-clip grey track; colour only inside the delivery window.

Timeline map (do not copy QB/baseball names onto cricket footage):
FP → **FFC**, BR → **REL**, FT → **FT**, MER → bowling-arm cocking,
PT → **BFC** if the trail ankle plants.

## Ball tracking (accuracy notes)

- Detection: dark-on-sky ovals **and** red/white cricket colour **and** frame-diff
  motion. Keep large motion-blurred ovals (training ball / football-shaped); do
  not require a 4px circle. Never search only a tiny box on the wrist — that
  locks onto the hand (~6 km/h garbage).
- **4K / high-res:** blob finders and CLAHE run on a working copy whose long
  side is ≤1920 (`detect.collect_flight_candidates`); `x,y,r` are mapped back
  to original pixels. Optical-flow validation downscales the same way. Compact
  radius is ~1% of the short side (a cricket ball, not a forearm). Pixel
  gates (hand exclusion, min step, net travel) scale with frame size so a
  ~17 px/frame body crawl cannot pass as a ball. Tracking seeds from the
  throw-peak wrist when refined REL has walked off the arm onto a poster.
  Ball tracking may walk back one wrist-teleport to seed the hand; it must
  not rewrite the pose speed series (that moves release and drops FFC / arm km/h).
- Tracking: seed on the object that has **left the hand** and is moving
  downrange; greedy-chain; refine the dark-blob centroid; **ballistic fit**
  `x(t)=x0+vx·t`, `y(t)=y0+vy·t+a·t²` for release speed (not raw per-frame jumps).
- Scale: user height × upright pose body px. **No fixed cm/px fallback**.
- Sanity band: ball speeds outside ~25–160 km/h are tracking noise — not reported.
- Front-on footage cannot yield true speed from 2D pixels — say so.

## Ball speed — the y-axis is noise, and `hypot` cannot tell

Measured on a real 4K/120 clip (`del_4c16280fbefa`, Aug 2026). The ball's
**horizontal** pixel track is clean (linear-fit residual ~5 px); its
**vertical** one is not (~19 px, and dy jumped 16.9 -> 86.8 -> 2.9 px between
consecutive frames, once going negative). At 4K the ball is a motion-blurred
streak a hundred-plus pixels long and the detector's centroid slides along it.

Consequences, all of which were live bugs:

- **`hypot(dx, dy)` per frame turns y-noise into speed.** Every frame that
  reported 130+ km/h was a y-spike; the true image-plane speed was ~86. The
  overlay was labelling the ball 132 km/h on the same frame the panel read
  100 — one render contradicting itself, which is how this was noticed.
- **Two estimators fed by the same corrupted signal do not cross-check each
  other.** The old code blended a quadratic endpoint-derivative (104.9) with
  the median of raw per-frame speeds (88.5) and reported 100. Both inputs were
  inflated by the same noise, so their agreement meant nothing.
- **Least squares cannot resist a streak-centroid outlier.** One point 124 px
  off the path bent the whole fit; residuals went 6.7 px -> 28.2 px and the
  trimming (median-based) failed because the outlier had already dragged the
  line it was being measured against.
- **The same noise corrupted the timebase.** Apparent gravity came out 11.62
  px/frame^2 against a physical 0.343 (34x), so the gravity-derived frame rate
  read 20.6 fps on a 120 fps clip.

The fix, in `track.robust_release_velocity_px_per_frame`:

1. **Pin gravity instead of fitting it.** Once `fps` and `mpp` are known,
   vertical acceleration in pixels is `9.81 / (mpp * fps**2)` — not a free
   parameter. Subtract `0.5*g*t**2` from y first; what remains is a straight
   line whose slope is vertical velocity *and nothing else*, so a y-outlier can
   no longer be absorbed as acceleration, i.e. as speed.
2. **Theil-Sen, not least squares.** The median of all pairwise slopes: an
   outlier corrupts only the `n-1` pairs it appears in out of `n(n-1)/2`, so
   the median does not move. O(n^2) over ~15 points is free.
3. **Never fit interpolated points.** `interpolate_gaps` fabricates positions
   to keep the drawn path continuous. They are a drawing aid; fitting them
   feeds the estimator its own guesses back as evidence.
4. **Refuse rather than guess.** The fit returns a `quality` from its own
   residuals; below `MIN_BALL_FIT_QUALITY` the metric goes `unavailable` with
   a reason. This caught a track whose fitted `vx` was *negative* and which
   had been displaying 63.24 km/h with confidence.

**There is one ball speed per delivery.** Do not put a per-frame instantaneous
number on the overlay next to a differently-computed headline — that is what
made this visible, and a value that swings 81-133 km/h on noise is not a
measurement worth showing.

Every reported speed is logged (`criclab.metrics`, "ball-speed | ...") with
fps, mpp, gravity, release frame, fit window, points used/dropped, residuals,
vx/vy, and quality — enough to reproduce the number from the frame data.

## Confidence & honesty

- Every physical metric that depends on scale should carry a **confidence** or quality flag
- UI and PDF must say **estimated** unless calibrated + validated
- Prefer failing a metric (null + reason) over inventing a precise number

## MongoDB usage

- One **delivery** (bowling action) document links to video, tracks, metrics, analysis
- Store paths to artifacts (overlay video, release still, PDF), not giant binaries in documents when avoidable
- Historical compare = query prior deliveries for **the same player**; ball speed is never compared to arm speed

## Agentic layer (Ollama)

Python helpers the runner actually calls (not Ollama tool-calling):

- `getDeliveryMetrics` — compact `status === ok` view; includes pace band, throwing screen, timebase, speed consistency
- `compareDeliveries` — ball-speed delta vs this bowler's prior deliveries; arm delta is a separate field
- `generateReport` — coaching narrative + catalog-only drill IDs

Planned (FEAT-015), not implemented — do not pretend they exist:

- `getPlayerHistory` (beyond same-player last-N compare)
- `searchCoachingMemory` (nomic embeddings)
- `detectOutliers`

Agent answers should cite measured differences, not vibes.

## PDF report structure (SpinLab-style, cricket labels)

1. **Bowling mechanics overview** — 2×2 labeled event stills (FFC, hip rotation,
   MER, REL) + headline metric tiles with reference-band bars
2. **Sequencing & key metrics** — kinematic sequence (back-foot → front-foot →
   arm cocking → release) + action score rings + extra tiles
3. **Joint angles** — bowling-arm & trunk/leg angle charts (matplotlib)
4. **Joint velocities** — hand-speed profile + angular-velocity charts
5. **Tabular data** — joint angles by phase + angular-velocity min/max
6. **Delivery summary** — stride (inches + % height), timing splits, speeds, scores
7. **AI coach notes** — Gemma summary/observations/strengths/improvements +
   catalog drill titles with YouTube URLs as text links (no iframes) +
   release-frame evidence image

## Cloudinary

Processed overlay video (`{job_id}_overlay`, folder `criclab/videos`) and PDF
(`{job_id}_report`, folder `criclab/reports`) are uploaded; the delivery stores
`artifacts.cloudinary_video_url` (playback, h264) and `cloudinary_pdf_url`. Upload
failures never fail the job — the app falls back to serving `/artifacts/...` from
local disk. Creds live in `.env` (`CLOUDINARY_URL` or the three explicit
fields).

## Anti-patterns (do not introduce)

- Using the LLM as the motion engine or to invent km/h / YouTube IDs
- Reporting ball speed from a hand/body/tree lock (e.g. 6 km/h) — only a moving in-air path counts
- Merging stump (Ball flight) speed into a side-on/front-on Action job as the headline
- Claiming radar-grade speed without calibration + validation (FEAT-016 still Planned)
- Reporting raw high-fps angular velocities without smooth + **reject** (do not clamp spikes)
- Running the backend on the 3.14 venv (mediapipe won't import)
- Building batting/fielding features before bowling MVP is solid
- Fat React components that reimplement backend metrics (UI is in criclab-web-frontend)
- Monolithic “analyze_everything.py” with no stage boundaries
- Reintroducing Notera (notes/PWA) or Next.js-as-frontend assumptions into this product
- Merging frontend source back into this repo (keep the split)

## MVP workflow checklist

1. Upload bowling video  
2. Pose estimation (MediaPipe) over the clip  
3. Detect throwing side (profile bowling_arm wins), release (leave-hand) + action phases  
4. Resolve scale from body height; calculate biomechanics metrics + scores (ok metrics only)  
5. Render slow-motion overlay video (pose + release + metrics)  
6. Upload processed video to Cloudinary (return shareable URL)  
7. Generate AI bowling analysis via Gemma (from metrics JSON only) + catalog drills  
8. Build SpinLab-style PDF with event stills + tables + drill URLs (+ upload to Cloudinary)  
9. Persist delivery in MongoDB; display on React results page with trajectory + DrillShelf  
