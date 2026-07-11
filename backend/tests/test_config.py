"""Tests for app.config — single-path pydantic-settings + LOCAL_TOKEN + CORS.

TDD: these tests are written BEFORE backend/app/config.py exists.
They MUST fail at collection (ModuleNotFoundError) and pass after implementation.

The module-level `settings` singleton is exercised only as a smoke probe; the
behavioural assertions construct a fresh ``Settings`` against a
``tmp_path``-based .env so each test is hermetic and independent of any real
.env the developer may have on disk.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import Settings, settings  # collection-time existence check

BACKEND_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = BACKEND_ROOT / "app"
CONFIG_FILE = APP_DIR / "config.py"
HEX32 = re.compile(r"[0-9a-f]{32}")


def _make_settings(env_file: Path) -> Settings:
    """Build a Settings pinned to a specific .env path.

    Centralises the one ``pyright: ignore`` needed because basedpyright
    cannot statically resolve the many private init-kwargs that
    ``pydantic-settings.BaseSettings`` adds dynamically (``_env_file``,
    ``_env_prefix``, etc.) — the runtime behaviour is correct, only the
    static analysis is overly strict.
    """
    return Settings(_env_file=str(env_file))  # pyright: ignore[reportCallIssue]


# --------------------------------------------------------------------------- #
# Test 1 — .env with SESSDATA + BILI_JCT → cookies dict has those keys.
# --------------------------------------------------------------------------- #
def test_cookies_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("SESSDATA=x\nBILI_JCT=y\n", encoding="utf-8")
    s = _make_settings(env_file)
    assert s.cookies == {"SESSDATA": "x", "bili_jct": "y"}


# --------------------------------------------------------------------------- #
# Test 2 — .env with BUVID3 too → cookies includes 'buvid3'.
# --------------------------------------------------------------------------- #
def test_cookies_includes_buvid3_when_set(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SESSDATA=x\nBILI_JCT=y\nBUVID3=z\n",
        encoding="utf-8",
    )
    s = _make_settings(env_file)
    assert s.cookies == {"SESSDATA": "x", "bili_jct": "y", "buvid3": "z"}


def test_cookies_omits_buvid3_when_unset(tmp_path: Path) -> None:
    """Negative case for Test 2 — keeps 'buvid3' out when not configured."""
    env_file = tmp_path / ".env"
    env_file.write_text("SESSDATA=x\nBILI_JCT=y\n", encoding="utf-8")
    s = _make_settings(env_file)
    assert "buvid3" not in s.cookies


# --------------------------------------------------------------------------- #
# Test 3 — LOCAL_TOKEN is non-empty, 32 hex chars, stable across accesses.
# --------------------------------------------------------------------------- #
def test_local_token_is_32_hex_chars(tmp_path: Path) -> None:
    s = _make_settings(tmp_path / "absent.env")
    token = s.LOCAL_TOKEN
    assert isinstance(token, str)
    assert token != ""
    assert HEX32.fullmatch(token), f"token {token!r} is not 32 lowercase hex chars"


def test_local_token_stable_across_accesses(tmp_path: Path) -> None:
    s = _make_settings(tmp_path / "absent.env")
    first = s.LOCAL_TOKEN
    second = s.LOCAL_TOKEN
    assert first == second, "LOCAL_TOKEN must be cached, not regenerated per access"


def test_local_token_stable_across_instances(tmp_path: Path) -> None:
    """Module-level cache means two Settings instances share the token."""
    a = _make_settings(tmp_path / "absent.env")
    b = _make_settings(tmp_path / "absent.env")
    assert a.LOCAL_TOKEN == b.LOCAL_TOKEN


# --------------------------------------------------------------------------- #
# Test 4 — cors_origins contains both Vite dev (:5173) and prod (:8000).
# --------------------------------------------------------------------------- #
def test_cors_origins_includes_dev_and_prod_ports(tmp_path: Path) -> None:
    s = _make_settings(tmp_path / "absent.env")
    origins = s.cors_origins
    assert isinstance(origins, list)
    assert any(":5173" in o for o in origins), f"missing :5173 origin in {origins}"
    assert any(":8000" in o for o in origins), f"missing :8000 origin in {origins}"


def test_cors_origins_covers_both_loopback_aliases(tmp_path: Path) -> None:
    """Momus Medium#3: allow both 'localhost' AND '127.0.0.1' for each port."""
    s = _make_settings(tmp_path / "absent.env")
    origins = s.cors_origins
    assert "http://localhost:5173" in origins
    assert "http://127.0.0.1:5173" in origins
    assert "http://localhost:8000" in origins
    assert "http://127.0.0.1:8000" in origins


# --------------------------------------------------------------------------- #
# Test 5 — Missing .env → defaults apply (no crash).
# --------------------------------------------------------------------------- #
def test_missing_env_file_uses_empty_defaults(tmp_path: Path) -> None:
    absent = tmp_path / "definitely_not_here.env"
    assert not absent.exists()
    s = _make_settings(absent)
    assert s.SESSDATA == ""
    assert s.BILI_JCT == ""
    assert s.BUVID3 is None
    assert s.ROOM_ID is None
    assert s.HOST == "127.0.0.1"
    assert s.PORT == 8000
    assert s.DEBUG is False


# --------------------------------------------------------------------------- #
# Test 6 — No dual-path / PyInstaller logic in the config module.
# --------------------------------------------------------------------------- #
def test_no_dual_path_logic_in_app() -> None:
    """The app package must not contain legacy dual-path config logic.

    Rejecting the ccShield config.py anti-pattern (lines 8-44) which branched
    on PyInstaller's bundled-binary detection (``sys.frozen`` /
    ``getattr(sys, 'frozen')``) and walked multiple candidate .env paths
    (``get_external_path`` / ``resource_path``). Single-path loading is the
    whole point of this module.

    The regex is PRECISE — it only matches the actual PyInstaller dual-path
    anti-patterns, NOT the Python stdlib ``frozenset`` type (or any other
    identifier that happens to start with ``frozen``). A false positive on
    ``frozenset[str]`` (T8 introduced in :mod:`app.api.middleware`) would
    defeat the purpose of the test as a regression guard.
    """
    if not CONFIG_FILE.exists():
        pytest.fail(f"{CONFIG_FILE} does not exist — TDD: write tests first, then impl")
    forbidden = re.compile(
        r"sys\.frozen|getattr\(sys,\s*['\"]frozen|get_external_path|resource_path"
    )
    findings: list[str] = []
    for path in APP_DIR.rglob("*.py"):
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if forbidden.search(line):
                findings.append(f"{path.relative_to(BACKEND_ROOT)}:{line_number}: {line}")

    assert not findings, "Found forbidden dual-path logic in backend/app/:\n" + "\n".join(
        findings
    )


# --------------------------------------------------------------------------- #
# Singleton smoke — importable, same identity across imports.
# --------------------------------------------------------------------------- #
def test_singleton_is_module_level_settings_instance() -> None:
    """The module exposes a `settings = Settings()` singleton."""
    from app import config as config_module

    assert isinstance(settings, Settings)
    assert config_module.settings is settings  # identity stable across imports
