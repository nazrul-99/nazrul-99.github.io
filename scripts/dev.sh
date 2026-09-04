#!/usr/bin/env bash
#
# Build, then watch content/ templates/ static/ and rebuild on change,
# serving the repo root on :8000. Runnable from any directory.
#
# Watching is a 1s poll over file mtimes and sizes -- no fswatch, no entr, no
# npm, per CLAUDE.md's hard constraints.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PORT="${PORT:-8000}"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
else
  echo "no python3 found" >&2
  exit 1
fi

fingerprint() {
  "$PY" - <<'PY'
import hashlib, pathlib
h = hashlib.sha1()
for directory in ("content", "templates", "static"):
    for path in sorted(pathlib.Path(directory).rglob("*")):
        if path.is_file() and not path.name.startswith("."):
            stat = path.stat()
            h.update(f"{path}:{stat.st_mtime_ns}:{stat.st_size}".encode())
print(h.hexdigest())
PY
}

"$PY" scripts/build.py

"$PY" -m http.server "$PORT" >/dev/null 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT INT TERM

echo
echo "  serving   http://localhost:$PORT"
echo "  watching  content/ templates/ static/"
echo "  ctrl-c to stop"
echo

last="$(fingerprint)"
while true; do
  sleep 1
  current="$(fingerprint)"
  if [[ "$current" != "$last" ]]; then
    last="$current"
    if "$PY" scripts/build.py; then
      echo "  rebuilt $(date '+%H:%M:%S')"
    else
      echo "  build failed -- fix and save again" >&2
    fi
  fi
done
