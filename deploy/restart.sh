#!/usr/bin/env bash
# Runs on the Lightsail instance after Actions rsyncs a new tree.
# Must run as the same user that owns PM2 (admin) — no sudo.
set -euo pipefail

ROOT="/var/www/criclab-web-backend"
cd "$ROOT"

if ! command -v python3.12 >/dev/null 2>&1; then
  echo "python3.12 is required (MediaPipe does not support 3.13+)." >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "Missing ${ROOT}/.env — create it on the instance. GitHub never deploys this file." >&2
  exit 1
fi

python3.12 -m venv .venv312
# shellcheck source=/dev/null
source .venv312/bin/activate
python -m pip install -q --upgrade pip
pip install -q -r requirements.txt

if ! command -v pm2 >/dev/null 2>&1; then
  echo "pm2 is not on PATH for $(whoami). Install: npm i -g pm2" >&2
  exit 1
fi

pm2 startOrReload "${ROOT}/deploy/ecosystem.config.cjs" --update-env
pm2 save

sleep 2
curl -fsS --max-time 20 http://127.0.0.1:8000/health
echo
echo "criclab-api restarted from ${ROOT}"
