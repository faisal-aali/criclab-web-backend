#!/usr/bin/env bash
# PM2 entrypoint. Uses the venv created by restart.sh — not system python.
set -euo pipefail
cd /var/www/criclab-web-backend
export PYTHONUNBUFFERED=1
exec .venv312/bin/python -m uvicorn app.main:app \
  --host 127.0.0.1 \
  --port 8000 \
  --workers 1 \
  --timeout-keep-alive 75
