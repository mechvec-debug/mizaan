#!/usr/bin/env bash
# Commit whatever changed in ./data_store and push it to the `data` branch. Safe when nothing changed; if another writer pushed first,
# the commit is replayed on top of theirs (never a force-push).
set -euo pipefail
DIR="${DATA_DIR:-data_store}"
BRANCH="${DATA_BRANCH:-data}"
MSG="${1:-data $(date -u +%F)}"
cd "$DIR"
git config user.name "${GIT_USER_NAME:-mizan-bot}"
git config user.email "${GIT_USER_EMAIL:-mizan-bot@users.noreply.github.com}"
printf 'tmp/\nlocks/\n' > .gitignore            # temp files and lock files are never committed
git add -A
if git diff --cached --quiet; then
  echo "no data changes to commit"
  exit 0
fi
git commit --quiet -m "$MSG"
for i in 1 2 3 4; do
  if git push --quiet origin "HEAD:$BRANCH"; then
    echo "pushed to $BRANCH (attempt $i)"
    exit 0
  fi
  git pull --quiet --rebase origin "$BRANCH" || true
  sleep $((i * ${PUSH_RETRY_SECONDS:-5}))
done
echo "could not push the data branch after retries" >&2
exit 1
