#!/usr/bin/env bash
# Make APP_DIR match origin/main. Token is used for this command only.
# Discards leftover rsync edits. Keeps gitignored files (.env, .venv312, storage).
set -euo pipefail

DIR="${1:?app directory}"
BRANCH="${2:-main}"
TOKEN="${GITHUB_TOKEN:?GITHUB_TOKEN is required}"
REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

cd "$DIR"

export GIT_TERMINAL_PROMPT=0
unset GIT_ASKPASS SSH_ASKPASS || true

git config --local --unset-all http.https://github.com/.extraheader >/dev/null 2>&1 || true

REMOTE="https://x-access-token:${TOKEN}@github.com/${REPO}.git"
git_c() {
  git -c "http.https://github.com/.extraheader=" -c "http.extraHeader=" "$@"
}

git_c fetch "$REMOTE" "$BRANCH"
# Untracked files from old rsync deploys block the update; ignored files stay.
git clean -fd
git checkout -f -B "$BRANCH" FETCH_HEAD

echo "Now at $(git log -1 --oneline)"
