#!/usr/bin/env bash
# Start the Cric-Lab FastAPI backend (Python 3.12 + uvicorn).
# Usage: ./run.sh   (from backend/)  or  backend/run.sh  (from repo root)

set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv312"
PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"

if ! command -v python3.12 &>/dev/null; then
  echo "python3.12 not found. Install Python 3.12." >&2
  exit 1
fi

if [[ ! -d "$VENV" ]]; then
  echo "Creating venv at $VENV ..."
  python3.12 -m venv "$VENV"
  # shellcheck source=/dev/null
  source "$VENV/bin/activate"
  pip install -q -r requirements.txt
else
  # shellcheck source=/dev/null
  source "$VENV/bin/activate"
fi

if [[ ! -f .env ]]; then
  echo "No .env — copying .env.example (edit Cloudinary / Mongo if needed)."
  cp .env.example .env
fi

echo "Cric-Lab API → http://${HOST}:${PORT}"
echo "Health: http://${HOST}:${PORT}/health"
python -c "from app.config import get_settings; print('Storage:', get_settings().storage_path)"
# python -m uvicorn (not the console script) so macOS spawn-reload can find stdlib.
exec python -m uvicorn app.main:app --reload --reload-dir app --reload-exclude '*.json' --reload-exclude '*.json.tmp' --reload-delay 0.75 --host "$HOST" --port "$PORT"
