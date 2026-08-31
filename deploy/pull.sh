#!/usr/bin/env bash
# APP_DIR must already be a git clone. CI token is used for this fetch only.
set -euo pipefail

cd "${1:?app directory}"
BRANCH="${2:-main}"
export GIT_TERMINAL_PROMPT=0

git fetch "https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" "$BRANCH"
git merge --ff-only FETCH_HEAD
echo "Now at $(git log -1 --oneline)"
