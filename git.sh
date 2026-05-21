#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
# shellcheck source=.env
set -a; source .env; set +a
git pull origin main
# ── 2. Create a new git branch ────────────────────────────────────────────────
BRANCH="data/$(date +%Y-%m-%d)"
# Append a counter if the branch already exists
if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
  echo ""
  echo "Using branch $BRANCH"
  git checkout "$BRANCH"
else
  echo ""
  echo "Creating branch: $BRANCH"
  git checkout -b "$BRANCH"
fi

bash pre-run.sh

# ── 3. Call run.sh ────────────────────────────────────────────────────────────
echo "Running run script..."
i=0
while git diff --exit-code in/
do
  echo "Running run script (ran $i times previously)"
  bash run.sh
  i=$((i+1))
  if git diff --exit-code in/; then
    echo "Failed to run script to completion, sleeping for 15 minutes before retrying..."
    sleep 0.25h
  fi
done
# ── 4. Commit generated changes ───────────────────────────────────────────────
echo ""
echo "Committing generated output..."

git add --all

if git diff --cached --quiet; then
  echo "No changes detected after generator run — nothing to commit."
else
  COMMIT_MSG="regenerate data files ($(date +%Y-%m-%d))"
  git commit -m "$COMMIT_MSG"
  echo ""
  echo "Committed on branch '$BRANCH': $COMMIT_MSG"
fi
# ── 5. Push changes and open merge request ────────────────────────────────────
echo "Pushing changes to remote..."
git push origin "$BRANCH"
REPO=$(git remote get-url origin | sed 's|.*github.com[:/]\(.*\)\.git|\1|;s|.*github.com[:/]\(.*\)|\1|')
gh pr create --base main --head "$BRANCH" --title "Automated data files update ($BRANCH)" --body "Automated PR to merge regenerated data files from branch \`$BRANCH\` into main.." --repo "$REPO"
# ── 6. Change working tree back to main branch, delete $BRANCH locally ─────────
git checkout main
git branch -D "$BRANCH"