#!/usr/bin/env bash
# scripts/check_secrets.sh — pre-commit gate for raw B站 credentials.
#
# Greps every git-tracked file for these patterns:
#
#   SESSDATA=[a-f0-9]{10,}        — Bilibili session cookie (lowercase key)
#   bili_jct=[a-f0-9]{20,}        — CSRF token in JSON/query-string form
#   BILI_JCT=[a-f0-9]{20,}        — CSRF token in .env-style form
#   DedeUserID=[0-9]{5,}          — numeric B站 user id
#
# Exit codes:
#   0  clean — no matches
#   1  secret-pattern match(es) found; offending file:line printed to stderr
#   2  not inside a git working tree
#
# Why this exists:
#   ccShield's COOKIE_AUTOBAN_SUMMARY.md:247 leaked a real SESSDATA into a
#   committed markdown report. reccshield's capture_fixtures.py redacts
#   before writing, but a future hand-edit, copy-paste, or `--no-verify`
#   slip could regress. This is the cheap, fast, network-free gate that
#   catches it BEFORE the commit lands.
#
# Allowlist escape hatch:
#   Lines containing the marker comment `# secretscan-allow` (or
#   `# secretscan: allow <reason>`) are skipped. Use this for synthetic
#   test fixtures that intentionally contain fake-but-hex-shaped patterns
#   (e.g., SESSDATA=deadbeefdeadbeef... in a redaction self-test).
#
#   Example:
#       "SESSDATA=deadbeefdeadbeefdeadbeefdeadbeef; "  # secretscan-allow: synthetic fixture
#
# Usage:
#   scripts/check_secrets.sh                # scan all tracked files
#   scripts/check_secrets.sh --paths FILE … # scan explicit files (CI override)
#
set -uo pipefail

# --- patterns ----------------------------------------------------------------
SESSDATA_RE='SESSDATA=[a-f0-9]{10,}'
BILI_JCT_LO_RE='bili_jct=[a-f0-9]{20,}'
BILI_JCT_HI_RE='BILI_JCT=[a-f0-9]{20,}'
DEDE_RE='DedeUserID=[0-9]{5,}'
ALLOW_MARKER_RE='secretscan-allow'

# Combined into one alternation so a single grep walks every file once.
# Order matters only for readability of the allowlist comment.
COMBINED_RE="${SESSDATA_RE}|${BILI_JCT_LO_RE}|${BILI_JCT_HI_RE}|${DEDE_RE}"

# --- locate repo root so the script works from any cwd ------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# --- enumerate input files ---------------------------------------------------
# Path-filter is defense-in-depth: these paths are .gitignore'd already and
# should never appear in `git ls-files`, but a broken .gitignore or a stray
# symlink could surface them.
is_excluded_path() {
  case "$1" in
    */.git/*|*/.git) return 0 ;;
    */node_modules/*|*/node_modules) return 0 ;;
    */dist/*|*/dist) return 0 ;;
    */.venv/*|*/.venv) return 0 ;;
    *) return 1 ;;
  esac
}

input_files=()
if [ "$#" -gt 0 ] && [ "${1:-}" = "--paths" ]; then
  shift
  for f in "$@"; do
    [ -f "$f" ] || continue
    is_excluded_path "$f" && continue
    input_files+=("$f")
  done
elif git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  while IFS= read -r -d '' f; do
    is_excluded_path "$f" && continue
    input_files+=("$f")
  done < <(git ls-files -z)
else
  echo "check_secrets: not inside a git working tree; pass --paths FILE … to scan explicitly." >&2
  exit 2
fi

if [ "${#input_files[@]}" -eq 0 ]; then
  echo "check_secrets: no files to scan."
  exit 0
fi

# --- grep --------------------------------------------------------------------
# printf '%s\0' emits a null-separated stream so filenames with spaces/newlines
# survive intact through xargs -0. grep -H prints filenames; -n prints line
# numbers; -I skips binary files.
matches=$(
  printf '%s\0' "${input_files[@]}" |
    xargs -0 grep -nHE -I --color=never "$COMBINED_RE" 2>/dev/null
  true
)

if [ -z "$matches" ]; then
  echo "check_secrets: clean (no SESSDATA/bili_jct/BILI_JCT/DedeUserID patterns)"
  exit 0
fi

# --- apply allowlist ---------------------------------------------------------
# A line is allowed iff its content (after file:line: prefix) contains the
# secretscan-allow marker. We keep the file:line prefix for reporting.
filtered=""
while IFS= read -r line; do
  # Strip the leading "path:lineno:" prefix to inspect content.
  content="${line#*:}"  # drops first field (path)
  content="${content#*:}"  # drops second field (lineno) — leaves the match text
  if printf '%s' "$content" | grep -qE "$ALLOW_MARKER_RE"; then
    continue
  fi
  filtered+="$line"$'\n'
done <<< "$matches"

if [ -z "$filtered" ]; then
  echo "check_secrets: clean (matches present but all carry # secretscan-allow)"
  exit 0
fi

# --- emit verdict ------------------------------------------------------------
echo "check_secrets: secret-pattern match(es) found — refusing to commit." >&2
echo "" >&2
echo "$filtered" >&2
echo "" >&2
echo "If a match is a KNOWN synthetic test fixture (e.g., a redaction self-test)," >&2
echo "suppress it by appending '# secretscan-allow: <reason>' to that line." >&2
exit 1