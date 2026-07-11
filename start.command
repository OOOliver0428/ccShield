#!/usr/bin/env sh

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT" || exit 1

/bin/sh "$ROOT/start.sh" "$@"
STATUS=$?

if [ "$STATUS" -ne 0 ]; then
  printf '\nStartup failed with code %s. Press Enter to close...\n' "$STATUS"
  read -r _unused
fi

exit "$STATUS"
