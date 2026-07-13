from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_launcher() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "start.py"
    spec = importlib.util.spec_from_file_location("ccshield_start", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_find_available_port_skips_occupied_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher()
    checked: list[int] = []

    def available(port: int) -> bool:
        checked.append(port)
        return port == 5175

    monkeypatch.setattr(launcher, "_port_is_available", available)

    assert launcher._find_available_port(5173, attempts=5) == 5175
    assert checked == [5173, 5174, 5175]


def test_find_available_port_fails_after_bounded_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher()
    monkeypatch.setattr(launcher, "_port_is_available", lambda _port: False)

    with pytest.raises(OSError, match="5173-5174"):
        launcher._find_available_port(5173, attempts=2)
