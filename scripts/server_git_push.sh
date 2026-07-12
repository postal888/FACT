#!/bin/bash
set -euo pipefail
cd /opt/factiva

git config user.name "postal888"
git config user.email "postal888@users.noreply.github.com"

# Remove accidental root index.html if templates version exists
if [ -f index.html ] && [ -f templates/index.html ]; then
  rm -f index.html
fi

git rm -r --cached . >/dev/null 2>&1 || true
git add .gitignore README.md .env.example requirements.txt deploy.sh \
  app.py twitter_poster.py telegram_poster.py techcrunch_feed.py factiva_agent.py \
  templates/index.html scripts/ 2>/dev/null || true

# Add scripts if present
if [ -d scripts ]; then
  git add scripts/*.py 2>/dev/null || true
fi

git status --short

if git diff --cached --quiet; then
  echo "Nothing to commit"
else
  git commit -m "Clean initial commit: Factiva Exporter"
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  git remote add origin git@github.com:postal888/FACT.git
fi

KEY=~/.ssh/github_fact_deploy
export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes"
git push -u origin main --force
echo "PUSH_OK"
