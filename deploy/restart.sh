#!/usr/bin/env bash
# Runs on the Lightsail instance after Actions rsyncs a new tree.
# Must run as the same user that owns PM2 (admin) — no sudo.
set -euo pipefail

ROOT="/var/www/criclab-web-backend"
cd "$ROOT"

pick_python() {
  local cmd ver
  if [[ -n "${PYTHON:-}" ]] && command -v "$PYTHON" >/dev/null 2>&1; then
    cmd="$PYTHON"
  else
    cmd=""
    for cand in python3.12 python3.11 python3.10 python3; do
      if command -v "$cand" >/dev/null 2>&1; then
        cmd="$cand"
        break
      fi
    done
  fi
  if [[ -z "$cmd" ]]; then
    echo "No Python 3 interpreter on PATH." >&2
    exit 1
  fi
  ver="$("$cmd" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  case "$ver" in
    3.10|3.11|3.12) echo "$cmd" ;;
    *)
      echo "Need Python 3.10–3.12 for MediaPipe (found $cmd = $ver)." >&2
      exit 1
      ;;
  esac
}

PY="$(pick_python)"
echo "Using $($PY --version 2>&1)"

if [[ ! -f .env ]]; then
  echo "Missing ${ROOT}/.env — create it on the instance. GitHub never deploys this file." >&2
  exit 1
fi

"$PY" -m venv .venv312
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
