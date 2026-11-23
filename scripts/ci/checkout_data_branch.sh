#!/usr/bin/env bash
# Check out the `data` branch into ./data_store (or start an empty one on the very first run).
set -euo pipefail
URL="${DATA_REPO_URL:-https://x-access-token:${GH_TOKEN:-}@github.com/${GITHUB_REPOSITORY:-}.git}"
DIR="${DATA_DIR:-data_store}"
BRANCH="${DATA_BRANCH:-data}"
rm -rf "$DIR"
if git ls-remote --exit-code --heads "$URL" "$BRANCH" >/dev/null 2>&1; then
  git clone --quiet --branch "$BRANCH" --single-branch --depth 1 "$URL" "$DIR"
  echo "data branch checked out ($(du -sh "$DIR" | cut -f1))"
else
  mkdir -p "$DIR"
  git -C "$DIR" init --quiet -b "$BRANCH"
  git -C "$DIR" remote add origin "$URL"
  echo "no data branch yet - starting a new one"
fi
