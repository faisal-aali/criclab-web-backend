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
| **Ball flight** | `/ball-flight` | ~4 m behind non-striker, **both stump sets** | Pitch-plane speed / line / length via stump homography (`backend/app/balltrack/`). Validate ~45–155 km/h. Not a radar gun. |

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
- **Front-foot contact** = lead-ankle plant 120–550 ms before release (omitted if not found).
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
- **2D hip/trunk rotation** is advanced proxy only — not headline truth. Reject values outside a cricket-plausible deg/s band (do not clamp spikes).
- **Profile `bowling_arm`** wins over auto side detection.

## Frontend API contract (sibling repo: criclab-frontend)

UI lives in `../criclab-frontend`. Backend must keep this contract stable:

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

Timeline map (do not copy QB/baseball names onto cricket footage):
FP → **FFC**, BR → **REL**, FT → **FT**, MER → bowling-arm cocking,
PT → **BFC** if the trail ankle plants.

## Ball tracking (accuracy notes)

- Detection: dark-on-sky ovals **and** red/white cricket colour **and** frame-diff
  motion. Keep large motion-blurred ovals (training ball / football-shaped); do
  not require a 4px circle. Never search only a tiny box on the wrist — that
  locks onto the hand (~6 km/h garbage).
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
- Historical compare = query prior deliveries for same player/session

## Agentic layer (Ollama)

Tools the agent may use (implement as real functions, not prompt fiction):

- `getDeliveryMetrics`
- `compareDeliveries`
- `getPlayerHistory`
- `searchCoachingMemory` (nomic embeddings)
- `detectOutliers`
- `generateReport` (structured sections for UI + PDF)

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
local disk. Creds live in `backend/.env` (`CLOUDINARY_URL` or the three explicit
fields).

## Anti-patterns (do not introduce)

- Using the LLM as the motion engine or to invent km/h / YouTube IDs
- Reporting ball speed from a hand/body/tree lock (e.g. 6 km/h) — only a moving in-air path counts
- Merging stump (Ball flight) speed into a side-on/front-on Action job as the headline
- Claiming radar-grade speed without calibration + validation (FEAT-016 still Planned)
- Reporting raw high-fps angular velocities without smooth + **reject** (do not clamp spikes)
- Running the backend on the 3.14 venv (mediapipe won't import)
- Building batting/fielding features before bowling MVP is solid
- Fat React components that reimplement backend metrics (UI is in criclab-frontend)
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
