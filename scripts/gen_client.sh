#!/usr/bin/env bash
# scripts/gen_client.sh — render the typed TS API client + types via
# @hey-api/openapi-ts, reading frontend/openapi.json (produced by
# scripts/gen_schema.sh) into frontend/src/api/generated/.
#
# The generated directory IS committed (the CI stale-gate diffs it). To
# pick up backend route changes, re-run scripts/gen_schema.sh and this
# script, then commit the diff.
#
# Requires: `bun` on PATH (frontend uses bun; this stays in its ecosystem).
# The @hey-api/openapi-ts package is installed as a frontend devDep so
# `bunx` resolves it from frontend/node_modules instead of fetching a
# fresh copy each run (deterministic + offline-friendly in CI after a
# `bun install --frozen-lockfile`).
#
# Exit codes:
#   0   client regenerated successfully
#   1   generator failed or produced no files
#   4   missing prerequisite (bun)
#
# Usage:
#   bash scripts/gen_client.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Ensure bun is reachable even in subshells that don't source ~/.bashrc.
export PATH="$HOME/.bun/bin:${PATH:-}"

if ! command -v bun >/dev/null 2>&1; then
  echo "gen_client: 'bun' not on PATH — install via https://bun.sh" >&2
  exit 4
fi

cd "$REPO_ROOT/frontend"

if [ ! -f "openapi.json" ]; then
  echo "gen_client: frontend/openapi.json missing — run scripts/gen_schema.sh first" >&2
  exit 1
fi

OUT_DIR="src/api/generated"

# Pristine regenerate: wipe everything (including stale `.gitkeep`) so the
# diff is purely "@hey-api/openapi-ts output vs HEAD".
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

# `bunx --bun` forces bun runtime for the spawned CLI (faster cold start).
# Generators: types + fetch-based sdk. No prettier post-process (would
# require prettier as a frontend dep, which we intentionally don't add).
if bunx --bun @hey-api/openapi-ts 2>&1 | tee /tmp/openapi-ts.$$.log; then
  :
else
  rc=$?
  echo "gen_client: @hey-api/openapi-ts failed (rc=$rc)" >&2
  echo "--- generator log ---" >&2
  cat /tmp/openapi-ts.$$.log >&2 || true
  rm -f /tmp/openapi-ts.$$.log
  exit 1
fi
rm -f /tmp/openapi-ts.$$.log

# Sanity: the generator's two signatures must exist. An empty diff would
# be a silent CI false-positive; this catches it.
missing=0
for f in "$OUT_DIR/types.gen.ts" "$OUT_DIR/sdk.gen.ts"; do
  if [ ! -s "$f" ]; then
    echo "gen_client: expected output missing or empty: $f" >&2
    missing=1
  fi
done
if [ "$missing" -ne 0 ]; then
  exit 1
fi

n=$(find "$OUT_DIR" -maxdepth 1 -type f | wc -l)
echo "gen_client: regenerated $OUT_DIR ($n top-level files)"
