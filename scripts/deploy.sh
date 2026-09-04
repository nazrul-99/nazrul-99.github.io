#!/usr/bin/env bash
#
# ./scripts/deploy.sh "commit message"
#
# Builds, validates, shows what would be committed, asks y/n, then commits and
# pushes. Refuses if validate fails, or if cv-full.pdf / *_draft* / *private* /
# *.docx is staged. Runnable from any directory.
#
# Note on order: CLAUDE.md's contract reads "validate, build". This runs build
# before validate, because validate checks the generated HTML -- validating
# first would test the *previous* build's output and can fail on stale files
# that the pending content already fixes. Nothing is committed until after
# validate passes and you answer y, so the safety property is unchanged.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -z "${1:-}" ]]; then
  echo 'usage: ./scripts/deploy.sh "commit message"' >&2
  exit 1
fi
MESSAGE="$1"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
else
  echo "no python3 found" >&2
  exit 1
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "refusing: $ROOT is not a git repository." >&2
  echo "Create the public <username>.github.io repo, clone it here, then retry." >&2
  exit 1
fi

# Anything forbidden that is already staged (only possible via 'git add -f',
# since .gitignore covers all four patterns).
FORBIDDEN_RE='(^|/)cv-full\.pdf$|_draft|private|\.docx$'

check_forbidden() {
  local staged
  staged="$(git diff --cached --name-only | grep -E "$FORBIDDEN_RE" || true)"
  if [[ -n "$staged" ]]; then
    echo "refusing: these staged files must never be committed:" >&2
    echo "$staged" | sed 's/^/    /' >&2
    echo >&2
    echo "unstage with: git restore --staged <file>" >&2
    exit 1
  fi
}

check_forbidden

echo "==> building"
"$PY" scripts/build.py

echo "==> validating"
if ! "$PY" scripts/validate.py; then
  echo >&2
  echo "refusing: validate.py failed. Nothing was committed." >&2
  exit 1
fi

echo "==> staging"
git add -A
check_forbidden   # again: git add -A must not have pulled anything in

BRANCH="$(git branch --show-current)"
if [[ "$BRANCH" != "main" ]]; then
  echo
  echo "warning: on branch '$BRANCH'; GitHub Pages serves main / root." >&2
fi

echo
echo "==> what will be committed"
if git diff --cached --quiet; then
  echo "    nothing staged -- working tree matches HEAD. Nothing to do."
  exit 0
fi
git -c color.status=always status --short
echo
git diff --cached --stat

echo
read -r -p "commit and push to $BRANCH? [y/N] " REPLY
if [[ "${REPLY:-}" != "y" && "${REPLY:-}" != "Y" ]]; then
  echo "aborted. Changes are still staged; nothing was committed."
  exit 1
fi

git commit -m "$MESSAGE"

if git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' >/dev/null 2>&1; then
  git push
else
  echo "no upstream set for '$BRANCH'; pushing with -u"
  git push -u origin "$BRANCH"
fi

echo
echo "pushed. GitHub Pages usually republishes within a minute."
