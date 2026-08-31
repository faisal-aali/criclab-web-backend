#!/usr/bin/env bash
# Fast-forward APP_DIR to origin/main. Token is used for this command only
# (never written into origin). Same auth as actions/checkout: x-access-token.
set -euo pipefail

DIR="${1:?app directory}"
BRANCH="${2:-main}"
TOKEN="${GITHUB_TOKEN:?GITHUB_TOKEN is required}"
REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

cd "$DIR"

export GIT_TERMINAL_PROMPT=0
unset GIT_ASKPASS SSH_ASKPASS || true

# Leftover extraheaders on self-hosted runners cause a password prompt.
git config --local --unset-all http.https://github.com/.extraheader >/dev/null 2>&1 || true

REMOTE="https://x-access-token:${TOKEN}@github.com/${REPO}.git"

git checkout "$BRANCH"
# Empty extraheader so a stale Authorization header is not sent with the URL token.
git -c "http.https://github.com/.extraheader=" -c "http.extraHeader=" \
  pull --ff-only "$REMOTE" "$BRANCH"

echo "Now at $(git log -1 --oneline)"
