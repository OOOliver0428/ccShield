"""Smoke tests for the FastAPI app factory."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import create_app


def test_create_app_returns_fastapi_instance() -> None:
    app = create_app()
    assert isinstance(app, FastAPI)


def test_health_endpoint_returns_ok() -> None:
    app = create_app()
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_release_static_build_is_served_from_same_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    static_dir = tmp_path / "frontend-dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(
        "<html><body>ccShield packaged UI</body></html>",
        encoding="utf-8",
    )
    (static_dir / "favicon.png").write_bytes(b"fake-png")
    monkeypatch.setenv("CCSHIELD_STATIC_DIR", str(static_dir))

    client = TestClient(create_app())
    root = client.get("/")
    favicon = client.get("/favicon.png")

    assert root.status_code == 200
    assert "ccShield packaged UI" in root.text
    assert favicon.status_code == 200
    assert favicon.content == b"fake-png"


def test_expired_runtime_cleanup_stops_the_active_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import room_routes
    from app.main import _stop_expired_authenticated_runtime

    stop_room = AsyncMock(return_value=room_routes.StopRoomResponse(ok=True))
    monkeypatch.setattr(room_routes, "stop_room_route", stop_room)

    asyncio.run(_stop_expired_authenticated_runtime())

    stop_room.assert_awaited_once()


# ---------------------------------------------------------------------------
# One-time .env migration on startup. Earlier releases wrote
# cookies to backend/.env (one level short of the project root), and
# config.py reads <repo>/.env. The fix is to migrate any pre-existing
# backend/.env into <repo>/.env the first time the new build boots, so
# users who already QR-scanned once don't have to re-scan after the
# upgrade. The helper is exposed as ``migrate_legacy_env`` on
# ``app.main`` for testability.
# ---------------------------------------------------------------------------


def test_migrate_legacy_env_moves_backend_env_to_repo_root(tmp_path: Path) -> None:
    """Given a legacy backend/.env and no <repo>/.env, the helper moves
    the legacy file to the project-root location and removes the old
    one. Idempotent: a second call is a no-op.
    """
    from app.main import migrate_legacy_env

    repo_root = tmp_path / "repo"
    backend = repo_root / "backend"
    backend.mkdir(parents=True)
    legacy_env = backend / ".env"
    legacy_env.write_text("SESSDATA=keep_me\n", encoding="utf-8")

    target_env = repo_root / ".env"

    # First call → moves.
    assert migrate_legacy_env(repo_root=repo_root) is True
    assert not legacy_env.exists()
    assert target_env.exists()
    assert "SESSDATA=keep_me" in target_env.read_text(encoding="utf-8")

    # Second call → no-op (file already at target).
    assert migrate_legacy_env(repo_root=repo_root) is False
    assert target_env.exists()


def test_migrate_legacy_env_no_op_when_legacy_absent(tmp_path: Path) -> None:
    """No backend/.env → no migration, no target file created."""
    from app.main import migrate_legacy_env

    repo_root = tmp_path / "repo"
    (repo_root / "backend").mkdir(parents=True)

    assert migrate_legacy_env(repo_root=repo_root) is False
    assert not (repo_root / ".env").exists()


def test_migrate_legacy_env_does_not_clobber_existing_target(tmp_path: Path) -> None:
    """<repo>/.env already exists → do NOT touch either file. We never
    want to clobber a freshly written target just because a stale legacy
    file is still hanging around.
    """
    from app.main import migrate_legacy_env

    repo_root = tmp_path / "repo"
    backend = repo_root / "backend"
    backend.mkdir(parents=True)
    legacy_env = backend / ".env"
    legacy_env.write_text("SESSDATA=OLD_LEGACY\n", encoding="utf-8")
    target_env = repo_root / ".env"
    target_env.write_text("SESSDATA=NEW_TARGET\n", encoding="utf-8")

    assert migrate_legacy_env(repo_root=repo_root) is False
    # Target unchanged.
    assert "SESSDATA=NEW_TARGET" in target_env.read_text(encoding="utf-8")
    # Legacy left in place — user can clean up manually.
    assert legacy_env.exists()
