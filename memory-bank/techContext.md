# Tech Context — Cric-Lab (Website API)

Sibling frontend: `../criclab-web-frontend` (Vite + React).
Sibling workers: `../criclab-video-service` (MediaPipe, overlay, PDF, Gemma video notes).

This repo is the **browser-facing FastAPI**: accounts, upload, job queue insert, results reads, Train catalog HTTP, chat assistant, bookings. It does **not** run the video pipeline.

## Stack

| Layer | Technology | Purpose |
|-------|------------|---------|
| API | **Python FastAPI** | Auth, upload, queue insert, job/delivery reads, admin |
| Database | **MongoDB** | Users, jobs, videos, deliveries (shared with workers) |
| OpenCV (stills) | `opencv-contrib-python-headless` | Ball-flight **stump photo** boxes only |
| Local LLM | **Ollama + `gemma3:4b`** | Website **chat assistant** (not video coaching notes) |
| Embeddings | **Ollama + `nomic-embed-text`** | Assistant RAG |
| Object storage | **Cloudinary** | Signed browser upload; overlay/PDF upload is the worker |
| Email | SMTP | OTP, recovery, notifications |

Video OpenCV, MediaPipe, matplotlib PDF, and drill matching live in `criclab-video-service`.

## Python environment

Use the dedicated 3.12 virtualenv (stump stills use OpenCV; MediaPipe is **not** a dependency here):

```bash
cd criclab-web-backend
source .venv312/bin/activate      # Windows: .\.venv312\Scripts\Activate.ps1
uvicorn app.main:app --reload --port 8000
```

Workers must also be running (`cd ../criclab-video-service && python -m app.worker`) or jobs stay `queued`.

## Why this backend

FastAPI is the only process the React app talks to. Heavy CV stays in a worker so closing a tab cannot stop a job, and the website process stays free for auth and polling.

## Local AI models

| Model | Role |
|-------|------|
| `gemma3:4b` | Chat assistant in this repo; video coaching notes in the worker |
| `nomic-embed-text:latest` | Embeddings for assistant retrieval |

## High-level data flow

```text
Vite React (upload)  [criclab-web-frontend]
  → FastAPI /videos or /balltrack  [this repo]
    → Mongo job status=queued
      → criclab-video-service worker claims
        → CV → overlay → Gemma notes + matching → PDF → delivery write
          → React polls this API /jobs + /deliveries
```

## Repo layout (this backend)

```text
criclab-web-backend/
├── app/
│   ├── api/              # routes (auth, videos, balltrack, coaching, admin, …)
│   ├── pipeline/         # ETA + player-profile parse (not pose/render)
│   ├── balltrack/        # stump still detect + session/job Mongo helpers
│   ├── coaching/         # drills.json catalog I/O for Train / admin
│   ├── agent/            # chat assistant LLM (not video coaching)
│   ├── assistant/        # RAG knowledge + chat
│   ├── services/         # cloudinary signed upload, email, bookings, …
│   ├── db/               # Mongo
│   ├── config.py
│   └── main.py
├── memory-bank/          # this folder
├── requirements.txt
├── .env.example
└── run.sh
```

Frontend: `criclab-web-frontend/`. Video CV: `criclab-video-service/`.

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

Pose drives mechanics. Ball km/h is a separate in-air measurement.

- **Arm/hand speed** at leave-hand (km/h + m/s)
- **Release** frame/point/height, **release time** (front-foot → release)
- **Arm-swing angular speed** (deg/s)
- **Hip / trunk rotation** (deg/s) — 2D side-on proxies, low confidence; reject outside a plausible band
- **Joint angles** per phase
- **Stride length** (% body height) at front-foot contact
- **Action scores** (heuristic 0–100)
- **Headline ball km/h** only when an in-air lock passes geometry + sanity gates
- **Pace band / throwing screen / speed consistency** — JSON + Results + PDF; screening refused unless the view earns it

Everything scale-dependent is labelled **estimated** until calibrated.

## Calibration

Pixel motion ≠ real-world speed. Always show confidence and label speeds as **estimates** until validated.

## Local setup

```bash
cd criclab-web-backend
python3.12 -m venv .venv312 && source .venv312/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000

# separate terminal — jobs stay queued without this
cd ../criclab-video-service && python -m app.worker

ollama list   # expect gemma3:4b, nomic-embed-text
```

## Constraints

- Do not put frame measurement logic in Gemma (and do not run the video pipeline in this process)
- Catalog HTTP stays here; drill matching after CV stays in `criclab-video-service`
- MongoDB is the system of record for analyses and history
- Frontend is a separate repo (`criclab-web-frontend`); do not reintroduce a monorepo layout here
- Mongo, Cloudinary, and `STORAGE_DIR` must match the video service
