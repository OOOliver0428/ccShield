from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_launcher() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "release.py"
    spec = importlib.util.spec_from_file_location("ccshield_release", script)
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
        return port == 8002

    monkeypatch.setattr(launcher, "port_is_available", available)

    assert launcher.find_available_port(8000, attempts=4) == 8002
    assert checked == [8000, 8001, 8002]


def test_default_data_dir_uses_local_app_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert launcher.default_data_dir() == tmp_path / "ccShield"


def test_configure_runtime_creates_private_data_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher()
    data_dir = tmp_path / "data"
    static_dir = tmp_path / "static"
    for key in (
        "CCSHIELD_RELEASE",
        "CCSHIELD_DATA_DIR",
        "CCSHIELD_STATIC_DIR",
        "HOST",
        "PORT",
        "DEBUG",
    ):
        monkeypatch.delenv(key, raising=False)

    launcher.configure_runtime(data_dir, static_dir, 8123)

    assert data_dir.is_dir()
    assert (data_dir / "logs").is_dir()
    assert os.environ["CCSHIELD_RELEASE"] == "1"
    assert os.environ["CCSHIELD_DATA_DIR"] == str(data_dir)
    assert os.environ["CCSHIELD_STATIC_DIR"] == str(static_dir)
    assert os.environ["HOST"] == "127.0.0.1"
    assert os.environ["PORT"] == "8123"


@pytest.mark.parametrize(
    ("app_url", "expected"),
    [
        (
            "http://127.0.0.1:8000",
            "ws://127.0.0.1:8000/api/ws/rooms/0?token=a%2Fb",
        ),
        (
            "https://localhost:8443",
            "wss://localhost:8443/api/ws/rooms/0?token=a%2Fb",
        ),
    ],
)
def test_websocket_url_is_same_origin_and_encodes_token(
    app_url: str,
    expected: str,
) -> None:
    launcher = _load_launcher()

    assert launcher.websocket_url(app_url, "a/b") == expected


def test_main_check_requires_bundled_frontend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher()
    monkeypatch.setattr(launcher, "bundle_root", lambda: tmp_path)
    monkeypatch.setattr(launcher, "port_is_available", lambda _port: True)

    assert launcher.main(["--check", "--data-dir", str(tmp_path / "data")]) == 1


def test_main_check_accepts_complete_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher()
    static_dir = tmp_path / "frontend" / "dist"
    static_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text("ccShield", encoding="utf-8")
    monkeypatch.setattr(launcher, "bundle_root", lambda: tmp_path)
    monkeypatch.setattr(launcher, "port_is_available", lambda _port: True)

    assert launcher.main(["--check", "--data-dir", str(tmp_path / "data")]) == 0
