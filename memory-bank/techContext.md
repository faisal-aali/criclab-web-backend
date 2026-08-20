# Tech Context — Cric-Lab (Backend)

Sibling frontend: `../criclab-frontend` (Vite + React). This repo is the FastAPI API + CV pipeline only.

## Stack

| Layer | Technology | Purpose |
|-------|------------|---------|
| Backend | **Python FastAPI** | Orchestration, upload APIs, job status, PDF generation endpoints |
| Pose engine | **MediaPipe BlazePose** (`opencv-contrib` + `mediapipe`) | 33 body landmarks/frame — the measurement engine |
| Video / CV | OpenCV (bundled ffmpeg) | Frame extraction, overlay render, MP4 (avc1/H.264) encode |
| Ball tracking | OpenCV MOG2 + RANSAC (best-effort) | Optional trajectory overlay only — NOT a headline metric |
| Motion engine | Custom Python metrics module | Arm speed, joint angles, timing, rotation proxies, scores |
| Charts | **matplotlib** (Agg) | Joint-angle / hand-speed charts embedded in the PDF |
| Database | **MongoDB** | Sessions, videos, deliveries, pose/metrics, analyses, agent runs |
| Local LLM | **Ollama + `gemma3:4b`** | Coaching insights, PDF narrative, comparisons |
| Embeddings | **Ollama + `nomic-embed-text`** | Semantic search over coaching notes / past observations |
| PDF | ReportLab (platypus + graphics) | SpinLab-style bowling report |
| Object storage | **Cloudinary** (creds in `.env`) + local disk fallback | Processed overlay video + PDF hosting; returns shareable URL |

## Python environment (IMPORTANT)

MediaPipe requires **Python 3.10–3.12** and pins `numpy<2`. Run the backend with
the dedicated 3.12 virtualenv, NOT a 3.13/3.14 one:

```bash
cd criclab-backend
source .venv312/bin/activate      # Windows: .\.venv312\Scripts\Activate.ps1
uvicorn app.main:app --reload --port 8000
```

Note: MediaPipe crashes under a restricted syscall sandbox but runs fine in a
normal shell.

## Why this backend

Python is the practical choice for OpenCV, pose estimation, tracking, and physics metrics. FastAPI exposes REST status to the Vite React app. The LLM never measures frames; it consumes structured JSON metrics.

## Local AI models

| Model | Role |
|-------|------|
| `gemma3:4b` | Reasoning, summaries, coaching copy for UI + PDF |
| `nomic-embed-text:latest` | Embeddings for historical coaching memory search |

## High-level data flow

```text
Vite React (upload)  [criclab-frontend]
  → FastAPI /videos + /analyses
    → FFmpeg / OpenCV pipeline
      → Detection + tracking + pose
        → Motion / physics engine → structured metrics
          → Ollama (gemma3:4b) agent tools
            → MongoDB persist
              → Results API + PDF generator
                → React results page
```

## Repo layout (this backend)

```text
criclab-backend/
├── app/
│   ├── api/              # routes (videos, balltrack, coaching, health)
│   ├── pipeline/         # extract · pose · action · calibrate · metrics · render
│   ├── balltrack/        # stump homography speed / line / length
│   ├── coaching/         # drills.json + recommend.py
│   ├── agent/            # Ollama tools + report narrative
│   ├── pdf/              # report builder + charts (matplotlib)
│   ├── services/         # cloudinary_service
│   ├── db/               # Mongo models / repositories
│   ├── config.py
│   └── main.py
├── storage/              # demo videos (gitignored); runtime uses ~/.local/share/criclab
├── memory-bank/          # this folder
├── requirements.txt
├── .env.example
└── run.sh
```

Frontend lives in the sibling repo `criclab-frontend/`.

## MongoDB collections (target)

| Collection | Purpose |
|------------|---------|
| `users` | Accounts (optional for early MVP) |
| `players` | Athlete profiles |
| `sessions` | Training sessions |
| `videos` | Video metadata + storage paths |
| `deliveries` | One doc per bowling delivery analyzed |
| `ball_tracks` | Frame-by-frame ball coordinates |
| `pose_tracks` | Body keypoints over time |
| `metrics` | Calculated bowling metrics + confidence |
| `analyses` | AI findings + PDF paths |
| `agent_runs` / `agent_steps` | Tool calls and reasoning traces |
| `coaching_notes` | Historical notes (embedded with nomic) |

## Bowling metrics (current)

Computed from the **pose track** (not ball tracking):

- **Arm/hand speed** at leave-hand (km/h + m/s)
- **Release** frame/point/height, **release time** (back-foot → release)
- **Arm-swing angular speed** (deg/s)
- **Hip / trunk rotation** (deg/s) — 2D side-on proxies, low confidence; reject outside a plausible band
- **Joint angles** per phase
- **Stride length** (% body height) at front-foot contact
- **Action scores** (heuristic 0–100)
- **Optional** ball trajectory overlay when a clean flight is tracked

Everything scale-dependent is labelled **estimated** until calibrated.

## Calibration

Pixel motion ≠ real-world speed. Always show confidence and label speeds as **estimates** until validated.

## Local setup

```bash
cd criclab-backend
python3.12 -m venv .venv312 && source .venv312/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000

ollama list   # expect gemma3:4b, nomic-embed-text
```

## Constraints

- Do not put frame measurement logic in Gemma
- Prefer modular pipeline packages over one giant script
- MongoDB is the system of record for analyses and history
- Frontend is a separate repo (`criclab-frontend`); do not reintroduce a monorepo layout here
