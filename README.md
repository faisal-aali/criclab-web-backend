# Cric-Lab Backend

FastAPI website API: accounts, uploads, job queue, chat assistant, coaching.

Video analysis (OpenCV / MediaPipe / overlay / PDF) runs in sibling
`criclab-video-service`. This API only stores the clip (or a Cloudinary URL)
and inserts a `queued` job. Workers claim it.

Sibling frontend: `../criclab-web-frontend` (Vite + React).

## Stack

| Layer | Tech |
|---|---|
| API | Python FastAPI |
| DB | MongoDB |
| Auth / email | JWT + SMTP |
| Chat | Ollama (local) or Amazon Bedrock (production) |
| Uploads | Local disk and/or Cloudinary signed browser upload |

## Prerequisites

- **Python 3.10–3.12** (OpenCV wheel used for stump-photo calibration).
- MongoDB (Atlas URI or local `mongod`)
- Ollama with `gemma3:4b` and `nomic-embed-text` when `APP_ENV=local`
- Cloudinary (optional; signed browser upload)
- **criclab-video-service workers** running, or jobs stay `queued`

## Quick start

```bash
cd criclab-web-backend
python3.12 -m venv .venv312
source .venv312/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

On macOS/Linux you can also use `./run.sh`.

### Health checks

- http://127.0.0.1:8000/health
- http://127.0.0.1:8000/health/mongo
- http://127.0.0.1:8000/health/llm

## Configuration

Copy `.env.example` → `.env`. This API needs Mongo, JWT, CORS, SMTP, and
Cloudinary for browser uploads. LLM keys are for the **chat assistant**.
Video-pipeline LLM keys live on `criclab-video-service`.

Mongo, Cloudinary, and `STORAGE_DIR` must match the video service.

**Never commit `.env`.**

## Production (Lightsail)

Push to `main` deploys via the self-hosted runner (FastAPI + PM2). Instance steps: [DEPLOY.md](DEPLOY.md).

Also run `criclab-video-service` on the same machine (same Mongo and `STORAGE_DIR`).
