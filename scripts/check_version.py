"""Verify that every user-visible ccShield version is synchronized."""

from __future__ import annotations

import argparse
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def versions() -> dict[str, str]:
    pyproject = tomllib.loads(
        (ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    )
    lockfile = tomllib.loads((ROOT / "backend" / "uv.lock").read_text(encoding="utf-8"))
    package = json.loads(
        (ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
    )
    app_source = (ROOT / "backend" / "app" / "__init__.py").read_text(
        encoding="utf-8"
    )
    launcher_source = (ROOT / "scripts" / "release.py").read_text(encoding="utf-8")

    app_match = re.search(r'^__version__ = "([^"]+)"$', app_source, re.MULTILINE)
    launcher_match = re.search(
        r'^RELEASE_VERSION = "([^"]+)"$', launcher_source, re.MULTILINE
    )
    if app_match is None or launcher_match is None:
        raise RuntimeError("could not parse Python version constants")
    locked_backend = next(
        package
        for package in lockfile["package"]
        if package.get("name") == "ccshield-backend"
    )
    return {
        "backend/pyproject.toml": str(pyproject["project"]["version"]),
        "backend/uv.lock": str(locked_backend["version"]),
        "backend/app/__init__.py": app_match.group(1),
        "frontend/package.json": str(package["version"]),
        "scripts/release.py": launcher_match.group(1),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--tag", required=True, help="release tag, e.g. v2.1.0")
    args = parser.parse_args()
    expected = args.tag.removeprefix("v")
    found = versions()
    mismatches = {path: value for path, value in found.items() if value != expected}
    if mismatches:
        for path, value in mismatches.items():
            print(f"version mismatch: {path}={value}, expected={expected}")
        return 1
    print(f"version check passed: {expected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
