# Cric-Lab Backend

FastAPI API for AI cricket bowling analysis: upload video → MediaPipe pose → metrics → slow-mo overlay → Gemma coaching → PDF report.

Sibling frontend: `../criclab-web-frontend` (Vite + React).

## Stack

| Layer | Tech |
|-------|------|
| API | Python FastAPI |
| DB | MongoDB |
| Vision | MediaPipe pose + OpenCV |
| Media | Slow-motion overlay (H.264) → Cloudinary (optional) |
| AI | Ollama `gemma3:4b` (+ `nomic-embed-text` for later memory) |
| PDF | ReportLab + matplotlib |

## Prerequisites

- **Python 3.10–3.12** (MediaPipe needs `numpy<2`; **not** 3.13/3.14). Use `.venv312`.
- MongoDB running locally (`mongod`)
- Ollama with models: `gemma3:4b`, `nomic-embed-text`
- Cloudinary account (optional; local disk fallback otherwise)

## Quick start

```bash
cd criclab-web-backend
python3.12 -m venv .venv312
# Windows PowerShell:
.\.venv312\Scripts\Activate.ps1
# macOS/Linux:
# source .venv312/bin/activate

pip install -r requirements.txt
cp .env.example .env   # then add Cloudinary / Mongo if needed
uvicorn app.main:app --reload --port 8000
```

On macOS/Linux you can also use `./run.sh`.

### Health checks

- http://127.0.0.1:8000/health
- http://127.0.0.1:8000/health/mongo
- http://127.0.0.1:8000/health/ollama

## Configuration

Copy `.env.example` → `.env`. Important keys:

- `CORS_ORIGINS` — must include the frontend origin (default `http://localhost:5173`). For a deployed frontend, add that origin.
- `STORAGE_DIR` — leave empty for `~/.local/share/criclab` (outside the repo).
- Cloudinary — `CLOUDINARY_URL` or the three explicit fields.

**Never commit `.env`.** It may contain API secrets.

## Production (Lightsail)

Push to `main` deploys via the self-hosted runner (FastAPI + PM2). Instance steps: [DEPLOY.md](DEPLOY.md).

## Demo / test videos

Sample clips live under `storage/videos/` (gitignored). Runtime uploads/artifacts use `STORAGE_DIR`, not this folder.

## Architecture

Read `memory-bank/` before extending features. Rule: **CV/physics measure first; Gemma reasons over structured metrics only.**
