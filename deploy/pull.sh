#!/usr/bin/env bash
# git pull --ff-only in the app directory. Does not reset or delete files.
set -euo pipefail

DIR="${1:?app directory}"
BRANCH="${2:-main}"
TOKEN="${GITHUB_TOKEN:-}"

git_auth() {
  if [[ -n "$TOKEN" ]]; then
    git -c "http.extraHeader=AUTHORIZATION: bearer ${TOKEN}" "$@"
  else
    git "$@"
  fi
}

cd "$DIR"
git_auth fetch origin "$BRANCH"
git checkout "$BRANCH"
git_auth pull --ff-only origin "$BRANCH"
echo "Now at $(git log -1 --oneline)"
