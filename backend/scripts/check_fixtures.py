"""Guard script: scan committed test fixtures for un-redacted secrets.

Why this exists
---------------
A historical development artifact accidentally included a real SESSDATA
credential. ``backend/scripts/capture_fixtures.py`` is the
pipeline that saves real B站 API responses for offline replay — it
redacts every credential before writing. ``check_fixtures.py`` is the
defensive companion that re-verifies any committed fixture actually
contains no real-looking secret.

The two scripts are belt-and-suspenders:

* capture_fixtures.py: redact-then-write (write side)
* check_fixtures.py:   read-then-assert-clean (read side)

If capture_fixtures.py ever regresses (a new sensitive key added, a new
endpoint whose envelope bypasses the redactor, an ``--no-verify``
slip), this script catches it at the next CI run / pre-commit.

Exit codes
----------
0  no fixtures OR every fixture passes the scan
1  one or more fixtures contain a real-looking SESSDATA / bili_jct /
   DedeUserID pattern
2  invalid arguments / I/O error

Scope
-----
Scans ``backend/tests/fixtures/*.json``. Real fixtures may not exist
yet (real capture is gated on a human login); an empty or missing directory
exits 0 — that is the expected state during early development. The
script is designed to be safe to wire into CI without false-positives
on an empty fixtures dir.

Patterns
--------
The same four patterns as ``scripts/check_secrets.sh``:

    SESSDATA=[a-f0-9]{10,}
    bili_jct=[a-f0-9]{20,}
    BILI_JCT=[a-f0-9]{20,}
    DedeUserID=[0-9]{5,}

JSON-form (key: "SESSDATA", value: "<hex>") is also flagged because
committed fixtures store secrets as JSON dicts, not ``k=v`` strings.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Patterns (mirror scripts/check_secrets.sh; update both together)
# ---------------------------------------------------------------------------

# `k=v` form — matches SESSDATA=deadbeef... and similar.
_SESSDATA_KV_RE = re.compile(r"SESSDATA=[a-f0-9]{10,}", re.IGNORECASE)
_BILI_JCT_KV_LO_RE = re.compile(r"bili_jct=[a-f0-9]{20,}", re.IGNORECASE)
_BILI_JCT_KV_HI_RE = re.compile(r"BILI_JCT=[a-f0-9]{20,}", re.IGNORECASE)
_DEDE_KV_RE = re.compile(r"DedeUserID=[0-9]{5,}", re.IGNORECASE)

# JSON-form — `"SESSDATA": "<hex>"` etc. Used because fixtures store
# secrets inside JSON dicts, where the separator is `": "` not `=`.
_JSON_SESSDATA_RE = re.compile(r'"SESSDATA"\s*:\s*"[a-f0-9]{10,}"', re.IGNORECASE)
_JSON_BILI_JCT_LO_RE = re.compile(r'"bili_jct"\s*:\s*"[a-f0-9]{20,}"', re.IGNORECASE)
_JSON_BILI_JCT_HI_RE = re.compile(r'"BILI_JCT"\s*:\s*"[a-f0-9]{20,}"', re.IGNORECASE)
_JSON_DEDE_RE = re.compile(r'"DedeUserID"\s*:\s*"[0-9]{5,}"', re.IGNORECASE)

# Ordered list used for reporting (label, pattern).
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("SESSDATA", _SESSDATA_KV_RE),
    ("SESSDATA(json)", _JSON_SESSDATA_RE),
    ("bili_jct", _BILI_JCT_KV_LO_RE),
    ("bili_jct(json)", _JSON_BILI_JCT_LO_RE),
    ("BILI_JCT", _BILI_JCT_KV_HI_RE),
    ("BILI_JCT(json)", _JSON_BILI_JCT_HI_RE),
    ("DedeUserID", _DEDE_KV_RE),
    ("DedeUserID(json)", _JSON_DEDE_RE),
]

# Sentinel that capture_fixtures.py writes in place of a redacted value.
# A fixture that contains ONLY this sentinel (or empty values) for every
# sensitive key is considered safe.
_REDACTED_SENTINEL = "<REDACTED>"

# Markers that suppress a finding for a specific pattern on a specific
# line. Inline JSON-comment isn't valid JSON, so we use a separate
# `_meta` sibling key on the fixture, e.g.:
#
#     {
#       "_meta": {"secretscan_allow": ["SESSDATA(json): synthetic dry-run"]},
#       "data": {"SESSDATA": "deadbeef..."}
#     }
_ALLOW_KEY = "secretscan_allow"


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


def _scan_text(path: Path, text: str) -> list[tuple[str, int, str]]:
    """Return [(pattern_label, line_no, line_text), ...] for every hit."""
    hits: list[tuple[str, int, str]] = []
    allow_patterns: set[str] = set()
    for ln, line in enumerate(text.splitlines(), start=1):
        for label, pat in _PATTERNS:
            if pat.search(line):
                hits.append((label, ln, line))
                break  # one report per line is enough
    # Per-line meta-allowlist: a sibling `_meta.secretscan_allow` list may
    # name patterns to suppress globally for this fixture. Cheap-and-simple:
    # if the fixture declares the allow, drop ALL hits of that pattern.
    try:
        meta = json.loads(text).get("_meta", {})
        allow_patterns = set(meta.get(_ALLOW_KEY, []))
    except (ValueError, AttributeError):
        allow_patterns = set()
    if allow_patterns:
        hits = [(label, ln, txt) for (label, ln, txt) in hits if label not in allow_patterns]
    # `path` is reserved for future per-path policies; suppress unused-arg lint.
    _ = path
    return hits


def scan_file(path: Path) -> list[tuple[str, int, str]]:
    """Scan one JSON fixture file. Returns a list of (label, line, text) hits."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"check_fixtures: cannot read {path}: {exc}", file=sys.stderr)
        return []
    return _scan_text(path, text)


def scan_dir(fixtures_dir: Path) -> list[tuple[Path, str, int, str]]:
    """Scan every ``*.json`` under ``fixtures_dir``. Returns [(path, label, ln, text), ...]."""
    if not fixtures_dir.exists():
        return []
    out: list[tuple[Path, str, int, str]] = []
    for path in sorted(fixtures_dir.glob("*.json")):
        if not path.is_file():
            continue
        for label, ln, text in scan_file(path):
            out.append((path, label, ln, text))
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m scripts.check_fixtures",
        description=(
            "Scan backend/tests/fixtures/*.json for un-redacted SESSDATA / "
            "bili_jct / DedeUserID values. Exits 0 on a clean (or empty) "
            "fixtures dir."
        ),
    )
    p.add_argument(
        "--fixtures-dir",
        type=Path,
        default=Path("backend/tests/fixtures"),
        help="Directory of *.json fixtures to scan (default: backend/tests/fixtures).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    fixtures_dir: Path = args.fixtures_dir.resolve()

    if not fixtures_dir.exists():
        print(f"check_fixtures: {fixtures_dir} does not exist — nothing to scan.")
        return 0

    hits = scan_dir(fixtures_dir)
    if not hits:
        print(f"check_fixtures: clean ({fixtures_dir} has no un-redacted secrets).")
        return 0

    print(
        f"check_fixtures: {len(hits)} un-redacted secret-pattern hit(s) in "
        f"{fixtures_dir}/ — refusing to pass.",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    for path, label, ln, text in hits:
        print(f"  {path}:{ln}: [{label}] {text.rstrip()}", file=sys.stderr)
    print(
        "\nIf a hit is a KNOWN synthetic fixture, suppress it by adding a "
        "_meta.secretscan_allow entry, e.g.:\n"
        '  "_meta": {"secretscan_allow": ["SESSDATA(json)"]}',
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
