#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 OWNER/brpt" >&2
  exit 2
fi

REPO="$1"

if ! command -v gh >/dev/null 2>&1; then
  echo "GitHub CLI (gh) is required. Install it from https://cli.github.com/" >&2
  exit 2
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "GitHub CLI is not authenticated. Run: gh auth login" >&2
  exit 2
fi

if ! git config user.name >/dev/null 2>&1 || ! git config user.email >/dev/null 2>&1; then
  cat >&2 <<'MSG'
Git author identity is not configured. Configure your own identity first, e.g.:

  git config --global user.name "Your Name"
  git config --global user.email "your-address@example.org"
MSG
  exit 2
fi

if gh repo view "${REPO}" >/dev/null 2>&1; then
  echo "GitHub repository ${REPO} already exists; refusing to overwrite it." >&2
  echo "Use scripts/init_git_repo.sh with its existing remote URL instead." >&2
  exit 2
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git init -b main
fi

git add .
if ! git diff --cached --quiet; then
  git commit -m "Initial BRPT research software repository"
fi

gh repo create "${REPO}" --public --source=. --remote=origin --push

echo
echo "Repository created and pushed: https://github.com/${REPO}"
