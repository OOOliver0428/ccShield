#!/usr/bin/env bash
# scripts/gen_schema.sh — boot the FastAPI backend, fetch /openapi.json,
# write it to frontend/openapi.json, then shut the backend down cleanly.
#
# Designed for both CI (job `openapi-stale`) and local pre-commit drift
# checks. Headless: no TTY, no interactive prompts, no orphaned processes.
#
# Env overrides:
#   RECCSHIELD_GEN_PORT  default 8000 — port to bind. Override when the
#                         developer already has the backend running on
#                         :8000 (this script REFUSES to kill a stranger's
#                         process to avoid disrupting dev flow).
#   RECCSHIELD_GEN_HOST  default 127.0.0.1 — bind host.
#   RECCSHIELD_GEN_WAIT  default 60 (≈30s) — max poll iterations at 0.5s.
#
# Exit codes:
#   0   schema fetched, validated, and written
#   1   backend did not become ready in time / fetch failed
#   2   response was not a valid OpenAPI 3.x document
#   3   target port already bound by someone else
#   4   missing prerequisite (uv, curl, python3, etc.)
#
# Examples:
#   bash scripts/gen_schema.sh
#   RECCSHIELD_GEN_PORT=8766 bash scripts/gen_schema.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

PORT="${RECCSHIELD_GEN_PORT:-8000}"
HOST="${RECCSHIELD_GEN_HOST:-127.0.0.1}"
WAIT_MAX="${RECCSHIELD_GEN_WAIT:-60}"   # 60 * 0.5s ≈ 30s
OUT_FILE="$REPO_ROOT/frontend/openapi.json"

LOG_FILE="$(mktemp -t gen_schema.XXXXXX.log)"
TMP_FILE="$(mktemp -t openapi.XXXXXX.json)"
PID=""

for tool in uv curl python3; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "gen_schema: required tool '$tool' not on PATH" >&2
    rm -f "$LOG_FILE" "$TMP_FILE"
    exit 4
  fi
done

cleanup() {
  local exit_code=$?
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    # Give uvicorn a brief window to shut down cleanly, then SIGKILL.
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$PID" 2>/dev/null || break
      sleep 0.2
    done
    if kill -0 "$PID" 2>/dev/null; then
      kill -9 "$PID" 2>/dev/null || true
    fi
  fi
  rm -f "$TMP_FILE"
  # Keep LOG_FILE only on failure so devs can see why; clear on success.
  if [ "$exit_code" -eq 0 ]; then
    rm -f "$LOG_FILE"
  else
    echo "gen_schema: uvicorn log retained at $LOG_FILE" >&2
  fi
  exit "$exit_code"
}
trap cleanup EXIT INT TERM

# Refuse to bind if a process already owns the port — never kill a
# stranger. CI sets RECCSHIELD_GEN_PORT to a unique value via the job.
if (exec 3<>"/dev/tcp/$HOST/$PORT") 2>/dev/null; then
  exec 3<&- 3>&-
  echo "gen_schema: $HOST:$PORT is already in use." >&2
  echo "  Stop the listener, or run: RECCSHIELD_GEN_PORT=<free> bash scripts/gen_schema.sh" >&2
  exit 3
fi

# Launch backend (background; capture logs for failure diagnosis only).
(cd "$REPO_ROOT/backend" && exec uv run uvicorn app.main:app --host "$HOST" --port "$PORT") \
  >>"$LOG_FILE" 2>&1 &
PID=$!

HEALTH_URL="http://$HOST:$PORT/openapi.json"
WAITED=0
while [ "$WAITED" -lt "$WAIT_MAX" ]; do
  if curl -sSf -o "$TMP_FILE" --max-time 2 "$HEALTH_URL" 2>/dev/null; then
    break
  fi
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "gen_schema: uvicorn exited prematurely" >&2
    echo "--- uvicorn log ---" >&2
    cat "$LOG_FILE" >&2 || true
    exit 1
  fi
  sleep 0.5
  WAITED=$((WAITED + 1))
done

if [ "$WAITED" -ge "$WAIT_MAX" ]; then
  echo "gen_schema: backend did not serve $HEALTH_URL within ~$((WAIT_MAX / 2))s" >&2
  echo "--- uvicorn log ---" >&2
  cat "$LOG_FILE" >&2 || true
  exit 1
fi

# Validate the response looks like an OpenAPI 3.x doc before committing.
if ! python3 - "$TMP_FILE" <<'PY'
import json
import sys
path = sys.argv[1]
with open(path) as f:
    d = json.load(f)
ver = d.get("openapi", "")
if not isinstance(ver, str) or not ver.startswith("3."):
    raise SystemExit(f"not openapi 3.x (got openapi={ver!r})")
if "paths" not in d or not isinstance(d["paths"], dict):
    raise SystemExit("missing 'paths' object")
PY
then
  echo "gen_schema: response at $HEALTH_URL is not a valid OpenAPI 3.x document" >&2
  exit 2
fi

mkdir -p "$(dirname "$OUT_FILE")"
mv "$TMP_FILE" "$OUT_FILE"
echo "gen_schema: wrote $OUT_FILE (port $PORT, $(wc -c < "$OUT_FILE") bytes)"
