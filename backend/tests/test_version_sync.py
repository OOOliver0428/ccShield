from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_checker() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "check_version.py"
    spec = importlib.util.spec_from_file_location("ccshield_check_version", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_all_release_version_sources_are_synchronized() -> None:
    checker = _load_checker()
    found = checker.versions()

    assert set(found.values()) == {"2.1.0"}
