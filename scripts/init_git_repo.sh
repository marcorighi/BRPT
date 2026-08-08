#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

if ! git config user.name >/dev/null 2>&1 || ! git config user.email >/dev/null 2>&1; then
  cat >&2 <<'MSG'
Git author identity is not configured. Configure your own identity first, e.g.:

  git config --global user.name "Your Name"
  git config --global user.email "your-address@example.org"

Then run this script again.
MSG
  exit 2
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git init -b main
fi

git add .
if ! git diff --cached --quiet; then
  git commit -m "Initial BRPT research software repository"
fi

if [[ $# -ge 1 ]]; then
  remote="$1"
  if git remote get-url origin >/dev/null 2>&1; then
    git remote set-url origin "${remote}"
  else
    git remote add origin "${remote}"
  fi
  git push -u origin main
else
  cat <<'MSG'
Local repository initialized and committed.

Create an EMPTY GitHub repository (do not add README, .gitignore or LICENSE),
then run, for example:

  ./scripts/init_git_repo.sh git@github.com:YOUR_ACCOUNT/brpt.git

or:

  git remote add origin https://github.com/YOUR_ACCOUNT/brpt.git
  git push -u origin main
MSG
fi
