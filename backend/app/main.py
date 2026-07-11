"""FastAPI application factory + uvicorn entrypoint.

The app factory wires together:

- the CORS middleware (allow-list from :data:`app.config.settings.cors_origins`),
- the LOCAL_TOKEN + Host-guard middleware from :mod:`app.api.middleware`,
- the top-level ``/api`` router (auth today; T13 adds room routes),
- a lifespan that (a) calls ``auth_session.check_on_startup()`` so the
  singleton reflects the on-disk cookies before the first request and
  (b) creates the shared ``httpx.AsyncClient`` used by the B站
  auth-flow route handlers.

``create_app()`` is the only thing :func:`app.main:app` (and every
test) calls; it must remain idempotent so a second ``create_app()`` in
the same process yields an independently-configured instance — that is
how the T8 test suite builds one app per test while sharing the
module-level ``auth_session`` mock.

Run locally with::

    uv run python -m app.main
"""
from __future__ import annotations

import os
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app import __version__
from app.api.middleware import LocalTokenMiddleware
from app.api.router import api_router
from app.auth import session as auth_session_module
from app.config import settings

# ---------------------------------------------------------------------------
# One-time .env migration
# ---------------------------------------------------------------------------
#
# Bug 1 / F3: an earlier release wrote cookies to ``backend/.env`` (one
# level short of the project root) while config.py read
# ``<repo>/.env`` — on restart the cookies were gone and the user had
# to re-scan. On the first boot of the fixed build, lift any pre-existing
# ``backend/.env`` into ``<repo>/.env`` so users who already QR-scanned
# once don't have to re-scan after the upgrade. The helper is exported
# (``migrate_legacy_env``) so test_app.py can pin its contract in
# isolation without spinning up the whole app.


def _default_repo_root() -> Path:
    """Project root: the parent of ``backend/``.

    Resolved from this file's location (``<repo>/backend/app/main.py``)
    by walking three parents up. Kept as a local helper so test
    fixtures can pass an explicit ``repo_root`` instead of touching
    the real on-disk tree.
    """
    return Path(__file__).resolve().parent.parent.parent


def migrate_legacy_env(
    *,
    repo_root: Path | None = None,
    backend_dir_name: str = "backend",
) -> bool:
    """Move ``<repo>/backend/.env`` → ``<repo>/.env`` if the legacy file
    exists AND no target file exists yet.

    Returns ``True`` when the migration actually moved a file,
    ``False`` otherwise (no legacy file, or a target already present).
    Never raises — a missing legacy file or a write failure is logged
    and the caller proceeds as if no migration was needed.

    Why "no clobber" semantics: a user who already wrote
    ``<repo>/.env`` with fresh cookies must not have those silently
    overwritten by an older ``backend/.env`` sitting on disk.

    Args:
        repo_root: project root to migrate within. Defaults to
            ``<repo>/`` derived from this file's location; tests pass
            an explicit ``tmp_path`` to avoid touching the real tree.
        backend_dir_name: name of the package directory the previous
            build mistakenly wrote into. Pinned to ``"backend"`` for
            testability; never use anything else in production.
    """
    root = repo_root if repo_root is not None else _default_repo_root()
    legacy: Path = root / backend_dir_name / ".env"
    target: Path = root / ".env"

    if not legacy.exists() or not legacy.is_file():
        return False
    if target.exists():
        logger.info(
            "migrate_legacy_env: legacy {} found but target {} already "
            "exists — leaving both files alone",
            legacy,
            target,
        )
        return False

    try:
        # ``shutil.move`` handles the cross-filesystem case by falling
        # back to copy+remove; for a same-filesystem move (the common
        # case) it's a rename. fsync the directory so a crash before
        # the directory entry commits doesn't leave a half-moved file.
        # Windows does not expose ``O_DIRECTORY`` and does not support
        # opening a directory this way, so the durability flush is a
        # POSIX-only best effort.
        shutil.move(str(legacy), str(target))
        directory_flag = getattr(os, "O_DIRECTORY", None)
        if directory_flag is not None:
            dir_fd = os.open(str(root), directory_flag)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except OSError as exc:
        logger.warning(
            "migrate_legacy_env: failed to move {} → {}: {}",
            legacy,
            target,
            exc,
        )
        return False

    logger.info(
        "migrate_legacy_env: moved legacy {} → {}",
        legacy,
        target,
    )
    return True


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown wiring.

    Startup:

    1. ``migrate_legacy_env`` — one-time lift of a pre-existing
       ``backend/.env`` into the canonical ``<repo>/.env`` location
       so users upgrading from a buggy prior build don't have to
       re-scan. No-op when nothing to migrate.
    2. ``auth_session.check_on_startup()`` — verify stored cookies via
       ``/x/web-interface/nav``. We access the singleton via its module
       (rather than a top-of-file ``from ... import auth_session``) so
       the test suite can ``monkeypatch.setattr`` the module-level
       binding and have the lifespan observe the mock immediately.
    3. Create the shared ``httpx.AsyncClient`` used by the B站
       auth-flow route handlers. A single instance keeps the
       connection pool warm across requests.

    Shutdown: close the ``httpx.AsyncClient`` so its connection pool
    drains cleanly.
    """
    migrate_legacy_env()
    await auth_session_module.auth_session.check_on_startup()
    app.state.http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(15.0, connect=5.0),
        headers={"User-Agent": "reccshield/0.1.0 (+local)"},
    )
    try:
        yield
    finally:
        await app.state.http_client.aclose()


def create_app() -> FastAPI:
    """Create and return a configured FastAPI application instance."""
    app = FastAPI(
        title="reccshield backend",
        version=__version__,
        description="Bilibili live-room moderator tool — backend API.",
        lifespan=lifespan,
    )

    # CORS — allow the Vite dev server (5173) and the production
    # static-serve port (8000) for both ``localhost`` and ``127.0.0.1``.
    # See ``app.config.Settings.cors_origins`` for the full allow-list.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    # LOCAL_TOKEN + Host-guard — runs AFTER CORS so preflight OPTIONS
    # requests are answered without an Authorization header.
    app.add_middleware(LocalTokenMiddleware)

    # Protected API surface. The middleware in :mod:`app.api.middleware`
    # enforces the host + token guard on every /api/* and /ws/* path.
    app.include_router(api_router)

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        """Lightweight health probe used by liveness/readiness checks.

        Exempt from the LOCAL_TOKEN + host guard (see
        :data:`app.api.middleware._EXEMPT_PATH_PREFIXES`) so a
        container orchestrator can probe the app without the bearer
        token.
        """
        return {"status": "ok"}

    logger.info(
        "create_app: reccshield backend v{} ready (cors_origins={!r}, "
        "host={!r}, port={!r})",
        __version__,
        settings.cors_origins,
        settings.HOST,
        settings.PORT,
    )
    return app


app: FastAPI = create_app()


if __name__ == "__main__":
    # Local development entrypoint. Production uses the WSGI/ASGI
    # server directly (e.g. ``uvicorn app.main:app --host 127.0.0.1``).
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
    )
