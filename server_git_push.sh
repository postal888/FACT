#!/bin/bash
set -euo pipefail
cd /opt/factiva

if [ ! -d .git ]; then
  git init -b main
fi

git config user.name "postal888"
git config user.email "postal888@users.noreply.github.com"

if [ ! -f .gitignore ]; then
cat > .gitignore <<'EOF'
.env
venv/
__pycache__/
exports/
publisher_stack.json
pipelines.json
published_stories.json
*.bak
*.session
tg_session.session
EOF
fi

git add -A
git status --short | head -40

if ! git diff --cached --quiet; then
  git commit -m "Initial commit from production server"
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  git remote add origin git@github.com:postal888/FACT.git
fi

KEY=~/.ssh/github_fact_deploy
if [ ! -f "$KEY" ]; then
  ssh-keygen -t ed25519 -N "" -f "$KEY" -C "factiva-deploy"
fi

echo "=== DEPLOY PUBLIC KEY (add to GitHub repo Deploy keys, write access) ==="
cat "${KEY}.pub"
echo "========================================================================"

export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
git push -u origin main
