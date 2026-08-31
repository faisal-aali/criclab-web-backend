#!/usr/bin/env bash
set -euo pipefail
if ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  cd "$ROOT"
else
  cd "$(dirname "$0")/.."
fi
git config core.hooksPath .githooks
chmod +x .githooks/pre-push
echo "Hooks installed (core.hooksPath=.githooks). git push runs CI checks first."
