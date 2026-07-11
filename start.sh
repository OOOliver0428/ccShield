#!/usr/bin/env sh
set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT" || exit 1

: "${UV_CACHE_DIR:=$ROOT/.uv-cache}"
export UV_CACHE_DIR
UV_PROJECT_ENVIRONMENT="$ROOT/backend/.venv"
export UV_PROJECT_ENVIRONMENT

find_uv() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return 0
  fi

  for candidate in \
    "$HOME/.local/bin/uv" \
    "$HOME/.cargo/bin/uv" \
    "/opt/homebrew/bin/uv" \
    "/usr/local/bin/uv"
  do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

UV_CMD=$(find_uv) || {
  printf '%s\n' '[start] uv was not found. Install uv and try again.' >&2
  exit 2
}

"$UV_CMD" sync --project backend --extra dev || {
  printf '%s\n' '[start] Backend dependency installation failed.' >&2
  exit 2
}

exec "$UV_PROJECT_ENVIRONMENT/bin/python" scripts/start.py "$@"
