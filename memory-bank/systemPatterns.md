# System Patterns — Cric-Lab

> **Most important Memory Bank file.** Read before writing code. These rules override vague prompts.

## Architecture rule #1

**Computer vision + physics produce measurements. The LLM coaches; it never measures.**

Never ask `gemma3:4b` to estimate speed/angles from raw video frames (that *increases* false km/h). There is no custom cricket LLM. Gemma runs *after* CV and only narrates `status === ok` JSON, then picks **catalog drill IDs**. Pipeline order (Action mode):

```text
Upload → Extract meta → POSE (MediaPipe) → Action/release detection → Calibrate
  → Best-effort ball track → Metrics JSON → Slow-mo overlay video (render)
  → Upload processed video to Cloudinary → Agent (gemma3:4b) narrative
  → SpinLab-style PDF (+ catalog drill URLs, no iframes) → Persist MongoDB → React results + Train
```

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
- **Ball speed (Action)** = ballistic vs in-air estimators on the path after release, × height scale.
  If they disagree by >35%, or view/geometry fail → unavailable. Sanity band ~25–160 km/h. Reject — never clamp.
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

## Frontend API contract (sibling repo: criclab-web-frontend)

UI lives in `../criclab-web-frontend`. Backend must keep this contract stable:

- Job polling via `/jobs/{id}` and `/balltrack/jobs/{id}` with `status`, `progress`, `stage`, `message`
- Metrics JSON: physical values only when `status === 'ok'` (null + reason otherwise); UI gates cards on that
- Relative artifact URLs (`/artifacts/...`, `/media/videos/...`) so the frontend can prefix with its API base
- CORS via `CORS_ORIGINS` must include the Vite origin (and any deployed frontend)
- React never talks to MongoDB or Ollama — only this FastAPI

## Backend (FastAPI)

- Thin route handlers: validate → enqueue/run pipeline → return job/analysis IDs
- Heavy work in `pipeline/` modules (extract, detect, track, calibrate, metrics)
- Agent lives in `agent/` and only receives structured tool outputs
- PDF generation is a separate module consuming metrics + analysis + optional frame stills

## Pipeline modularity

Each stage is swappable:

| Stage | Module | Input | Output |
|-------|--------|-------|--------|
| Extract | `pipeline/extract.py` | video path | frames / fps / metadata |
| Pose | `pipeline/pose.py` | frames | 33 landmarks/frame (pixels + visibility) |
| Action | `pipeline/action.py` | pose track | throwing side, release frame, phases, wrist-speed series |
| Calibrate | `pipeline/calibrate.py` | body px height + ref | px → world scale |
| Ball (opt.) | `pipeline/detect.py`+`track.py` | frames | best-effort trajectory (≥6 pts) |
| Metrics | `pipeline/metrics.py` | pose + action + scale | metrics doc + confidence + scores |
| Render | `pipeline/render.py` | video + pose + metrics | slow-mo overlay MP4 (avc1) + release still |
| Upload | `services/cloudinary_service.py` | mp4 + pdf | Cloudinary secure/playback URLs |
| Agent | `agent/ollama_agent.py` | metrics + catalog drills | narrative + `recommendations` (catalog IDs only) |
| Coaching | `coaching/drills.json` + `recommend.py` | scores / line-length | weakness tags; never invent URLs |
| PDF | `pdf/report.py` + `pdf/charts.py` | metrics + analysis + charts | SpinLab-style PDF (event stills + drill text links) |
| Ball flight | `balltrack/` | behind-bowler + stump boxes | pitch-plane speed, line, length, overlay, pitch map |

Add new bowling metrics by extending the metrics stage + UI cards + PDF tiles —
do not rewrite upload/API shells.

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
