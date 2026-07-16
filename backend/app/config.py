"""Application configuration for source and packaged runtimes.

There is one canonical data directory per process. Source runs default to the
repository root; the packaged launcher sets ``CCSHIELD_DATA_DIR`` to the
current user's local application-data directory before importing the app.
Configuration code therefore remains independent from PyInstaller internals
and never walks a list of ambiguous candidate paths.

Anti-pattern reference: the previous-generation config walked four
candidate .env paths and loaded via python-dotenv only when present.
We instead let pydantic-settings do the load via ``env_file`` and let
missing files silently fall through to declared defaults.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root is the parent of ``backend/``, i.e. ``<repo>/``.
# This file lives at ``<repo>/backend/app/config.py`` → three levels up.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = Path(os.environ.get("CCSHIELD_DATA_DIR", _PROJECT_ROOT)).expanduser().resolve()
_ENV_FILE = DATA_DIR / ".env"

# Process-lifetime cache for LOCAL_TOKEN. Module-level so the value persists
# across all Settings instances and all accesses — NOT regenerated per request.
_LOCAL_TOKEN_CACHE: str | None = None


class Settings(BaseSettings):
    """Bilibili live-room moderator tool — runtime configuration.

    Fields are read from the project-root ``.env`` via ``pydantic-settings``.
    All fields have safe defaults so a missing or partial .env never crashes
    startup; business code that needs SESSDATA / BILI_JCT should treat empty
    strings as "not logged in" and prompt the QR flow.
    """

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # --- Bilibili authentication cookies --------------------------------- #
    SESSDATA: str = ""
    BILI_JCT: str = ""
    BUVID3: str | None = None

    # --- Target live room ------------------------------------------------- #
    ROOM_ID: int | None = None

    # --- Server bind ------------------------------------------------------ #
    HOST: str = "127.0.0.1"
    PORT: int = 8000
    DEBUG: bool = False

    # --------------------------------------------------------------------- #
    # Computed properties
    # --------------------------------------------------------------------- #

    @property
    def cookies(self) -> dict[str, str]:
        """Bilibili API cookie dict.

        Always includes ``SESSDATA`` and ``bili_jct``; ``buvid3`` is added
        only when BUVID3 was configured. Lowercase keys match what the B站
        web API expects when these cookies are replayed on requests.
        """
        result: dict[str, str] = {
            "SESSDATA": self.SESSDATA,
            "bili_jct": self.BILI_JCT,
        }
        if self.BUVID3:
            result["buvid3"] = self.BUVID3
        return result

    @property
    def LOCAL_TOKEN(self) -> str:
        """Process-lifetime bearer token for the local HTTP/WS API.

        Generated lazily on first access and cached at module scope so the
        value is stable across requests, across ``Settings()`` instances, and
        across ``from app.config import settings`` re-imports within the
        same Python process. ``secrets.token_hex(16)`` yields 32 lowercase
        hex characters with 128 bits of entropy.
        """
        global _LOCAL_TOKEN_CACHE
        if _LOCAL_TOKEN_CACHE is None:
            _LOCAL_TOKEN_CACHE = secrets.token_hex(16)
        return _LOCAL_TOKEN_CACHE

    @property
    def cors_origins(self) -> list[str]:
        """CORS allow-list for the local FastAPI app.

        Includes both ``localhost`` and ``127.0.0.1`` for each port so the
        Vite dev server (``:5173``) and the production static-serve port
        (``:8000``) are both reachable regardless of which loopback alias
        the browser normalises to.

        Momus Medium#3 fix: the original ccShield CORS config only accepted
        ``localhost:8000``, which silently blocked Vite-served dev pages
        when the browser sent the request via ``127.0.0.1:5173`` (or vice
        versa). Both aliases must be present.
        """
        return [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:8000",
            "http://127.0.0.1:8000",
        ]


# Module-level singleton — import as ``from app.config import settings``.
# Constructed once at import time, picks up the project-root .env.
settings = Settings()
