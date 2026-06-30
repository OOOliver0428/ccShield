"""HTTP auth-flow route handlers.

Endpoints (all live under ``/api/auth/*`` once the :data:`api_router`
prefix is applied):

- ``POST /api/auth/qr/start``  — call :func:`qr_generate` and return the
  ``{qrcode_url, qrcode_key}`` pair so the UI can render the QR.
- ``GET  /api/auth/qr/poll``   — call :func:`qr_poll` and translate the
  four B站 poll-state outcomes (``scanning``, ``confirmed``, ``expired``,
  ``success``) into a single ``{status: ...}`` envelope. On success
  we atomically persist the new cookies and re-fire
  :meth:`AuthSession.check_on_startup` so the singleton reflects the
  fresh state.
- ``GET  /api/auth/status``    — return the current
  :class:`AuthState` value as a string.
- ``POST /api/auth/manual``    — Plan B fallback. Validate user-pasted
  SESSDATA / bili_jct / buvid3 via :func:`save_cookies_manual`. On
  :class:`LoginIncompleteError` return HTTP 400.

All endpoints share one ``httpx.AsyncClient`` (held on
``app.state.http_client``) so connection pooling, DNS resolution, and
keep-alive are reused across requests. The lifespan in
:mod:`app.main` creates the client on startup and closes it on shutdown.

Test seams:

- :data:`_ENV_PATH` — module-level ``Path`` pointing at the project
  root's ``.env``. Tests monkeypatch this to a ``tmp_path`` to assert
  what gets written without touching the real on-disk file.
- The auth functions and ``auth_session`` are imported by name; tests
  use :func:`monkeypatch.setattr` on this module to swap them out.

Strict typing: every parameter and return value carries an explicit
type. ``Any`` is not used.
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from app.auth import session as auth_session_module
from app.bilibili.auth import (
    LoginIncompleteError,
    QrAwaitingConfirmError,
    QrAwaitingScanError,
    QrExpiredError,
    qr_generate,
    qr_poll,
    save_cookies_manual,
    write_env_atomic,
)
from app.config import settings

# ---------------------------------------------------------------------------
# Module-level paths and dependencies
# ---------------------------------------------------------------------------

# Project root is the parent of ``backend/``, i.e. ``<repo>/``. This file
# lives at ``<repo>/backend/app/api/auth_routes.py`` → four levels up.
_PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent
_ENV_PATH: Path = _PROJECT_ROOT / ".env"


def get_http_client(request: Request) -> httpx.AsyncClient:
    """FastAPI dependency: return the shared ``httpx.AsyncClient``.

    The lifespan in :mod:`app.main` is responsible for placing a fresh
    client on ``app.state.http_client`` on startup and closing it on
    shutdown. If the lifespan did not run (e.g. in a misconfigured
    test) the route raises HTTP 503 — a developer-visible signal that
    the wiring is wrong, not a silent 500.
    """
    client: httpx.AsyncClient | None = getattr(request.app.state, "http_client", None)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="http client not initialised — lifespan did not run",
        )
    return client


# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------


class QrStartResponse(BaseModel):
    """Response body for ``POST /api/auth/qr/start``."""

    model_config = ConfigDict(extra="forbid")

    qrcode_url: str
    qrcode_key: str


class QrPollResponse(BaseModel):
    """Response body for ``GET /api/auth/qr/poll``.

    ``status`` is one of:

    - ``"scanning"`` — B站 code 86101; the user has not scanned the QR yet.
    - ``"confirmed"`` — B站 code 86090; scanned, awaiting phone confirm.
    - ``"expired"``  — B站 code 86038; the QR expired, regenerate.
    - ``"success"``  — login complete; cookies are now persisted.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["scanning", "confirmed", "expired", "success"]


class AuthStatusResponse(BaseModel):
    """Response body for ``GET /api/auth/status``."""

    model_config = ConfigDict(extra="forbid")

    state: str


class ManualCookiesRequest(BaseModel):
    """Request body for ``POST /api/auth/manual`` (Plan B)."""

    model_config = ConfigDict(extra="forbid")

    sessdata: str = Field(min_length=1)
    bili_jct: str = Field(min_length=1)
    buvid3: str | None = None


class ManualCookiesResponse(BaseModel):
    """Response body for ``POST /api/auth/manual`` on success."""

    model_config = ConfigDict(extra="forbid")

    uname: str
    mid: int


class BootstrapResponse(BaseModel):
    """Response body for ``GET /api/auth/bootstrap``.

    Returns the current :attr:`Settings.LOCAL_TOKEN` so the frontend can
    attach it as ``Authorization: Bearer …`` on every subsequent call.
    The route is exempted from the LOCAL_TOKEN gate (see
    :data:`app.api.middleware._EXEMPT_PATH_PREFIXES`) but the Host guard
    still applies — only a page served from ``localhost`` / ``127.0.0.1``
    can lift the token, which is the single-user-local trust model.
    """

    model_config = ConfigDict(extra="forbid")

    token: str


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router: APIRouter = APIRouter(prefix="/auth", tags=["auth"])


# Type alias for the Depends() injection on every route that needs the
# shared HTTP client. Keeps the per-route signatures consistent.
HttpClientDep = Annotated[httpx.AsyncClient, Depends(get_http_client)]


@router.get(
    "/bootstrap",
    response_model=BootstrapResponse,
    summary="Hand the frontend the LOCAL_TOKEN (single-user-local bootstrap)",
)
async def auth_bootstrap() -> BootstrapResponse:
    """Return the current ``LOCAL_TOKEN`` so the SPA can attach it to
    every subsequent ``/api/*`` call.

    The endpoint is **exempt from the LOCAL_TOKEN gate** (see
    :data:`app.api.middleware._EXEMPT_PATH_PREFIXES`) — otherwise the
    frontend would have no way to learn the token on a freshly opened
    page. The Host guard still runs: only a request whose Host is
    ``localhost`` or ``127.0.0.1`` can lift the token, so an external
    origin cannot ride the CORS allow-list to read it.

    Trust model: LOCAL_TOKEN is a single-user-local secret whose job is
    CSRF / DNS-rebinding defence, not secrecy from the developer's own
    machine. Anything that can serve a page from ``localhost`` already
    has full read/write access to the same machine's cookies.
    """
    return BootstrapResponse(token=settings.LOCAL_TOKEN)


@router.post(
    "/qr/start",
    response_model=QrStartResponse,
    summary="Start the B站 QR-login flow",
)
async def qr_start(client: HttpClientDep) -> QrStartResponse:
    """Call :func:`qr_generate` and return the QR code payload.

    The UI renders the QR (an image) and the user scans it with the
    B站 mobile app. The returned ``qrcode_key`` is opaque to the
    client — it is passed back on each ``/qr/poll`` call.
    """
    payload: dict[str, str] = await qr_generate(client)
    return QrStartResponse(
        qrcode_url=payload["qrcode_url"],
        qrcode_key=payload["qrcode_key"],
    )


@router.get(
    "/qr/poll",
    response_model=QrPollResponse,
    summary="Poll B站 for the QR-login state",
)
async def qr_poll_route(
    client: HttpClientDep,
    qrcode_key: str,
) -> QrPollResponse:
    """Poll B站 for the current state of an in-flight QR login.

    Translates the B站 error codes into a single ``status`` field:

    - :class:`QrAwaitingScanError`    → ``scanning``
    - :class:`QrAwaitingConfirmError` → ``confirmed``
    - :class:`QrExpiredError`         → ``expired``
    - success path                    → ``success`` + persist + state refresh

    Any other exception is re-raised — those signal a real backend
    problem (network down, B站 format change) and the client should
    surface them as 5xx.
    """
    try:
        payload = await qr_poll(client, qrcode_key)
    except QrAwaitingScanError:
        return QrPollResponse(status="scanning")
    except QrAwaitingConfirmError:
        return QrPollResponse(status="confirmed")
    except QrExpiredError:
        return QrPollResponse(status="expired")

    # success path: persist cookies and refresh the in-memory auth state.
    sessdata: str = payload["sessdata"]
    bili_jct: str = payload["bili_jct"]
    write_env_atomic(
        sessdata=sessdata,
        bili_jct=bili_jct,
        buvid3=None,
        env_path=_ENV_PATH,
    )
    # Hot-reload the auth session: write the new cookies into the live
    # ``settings`` singleton AND re-fire the startup check so the state
    # machine reaches AUTHENTICATED without a process restart. See
    # :meth:`AuthSession.mark_authenticated_after_login` for the full
    # rationale (in short: ``check_on_startup`` reads from
    # ``settings.SESSDATA`` / ``settings.BILI_JCT``, which still hold
    # the import-time empty values until we mutate them).
    try:
        await auth_session_module.auth_session.mark_authenticated_after_login(
            sessdata=sessdata,
            bili_jct=bili_jct,
            buvid3=None,
        )
    except Exception:
        # State-refresh failure MUST NOT mask the successful login — the
        # .env is already on disk, the user IS authenticated on the next
        # process restart; we just couldn't reach that state in this
        # same process. Log and move on.
        logger.exception(
            "auth_routes.qr_poll_route: post-success state refresh failed"
        )
    return QrPollResponse(status="success")


@router.get(
    "/status",
    response_model=AuthStatusResponse,
    summary="Read the current auth state",
)
async def auth_status() -> AuthStatusResponse:
    """Return the current :class:`AuthState` value as a string.

    The state is read from the :data:`app.auth.session.auth_session`
    singleton on every call (no caching), so the response always
    reflects the latest evaluation.
    """
    # We deliberately access the module-level ``auth_session`` via its
    # module rather than a top-of-file ``from ... import auth_session``.
    # That way a test can ``monkeypatch.setattr`` on
    # ``app.auth.session.auth_session`` and the route sees the mock
    # immediately, even after this module has been imported.
    return AuthStatusResponse(state=auth_session_module.auth_session.state.value)


@router.post(
    "/manual",
    response_model=ManualCookiesResponse,
    summary="Save user-pasted cookies (Plan B fallback)",
)
async def manual_cookies(
    body: ManualCookiesRequest,
    client: HttpClientDep,
) -> ManualCookiesResponse:
    """Validate pasted SESSDATA / bili_jct / buvid3 and persist on success.

    On :class:`LoginIncompleteError` (the pasted cookies fail the
    ``/nav`` validation or the response is malformed) we surface
    HTTP 400. The caller's existing ``.env`` is left untouched — the
    :func:`save_cookies_manual` helper does the same guarantee at the
    persistence layer.
    """
    try:
        result = await save_cookies_manual(
            client=client,
            sessdata=body.sessdata,
            bili_jct=body.bili_jct,
            buvid3=body.buvid3,
            env_path=_ENV_PATH,
        )
    except LoginIncompleteError as exc:
        # 400 (Bad Request) — the client supplied cookies that B站
        # rejected. 401 would be misleading (this is not an
        # authentication-of-the-API-caller problem; the API caller's
        # token was already validated by the middleware).
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    # Validation succeeded and .env is on disk — hot-reload the live
    # ``settings`` singleton + re-fire the startup check so
    # ``GET /auth/status`` reports ``authenticated`` in this same
    # process. See :meth:`AuthSession.mark_authenticated_after_login`
    # for the rationale.
    try:
        await auth_session_module.auth_session.mark_authenticated_after_login(
            sessdata=body.sessdata,
            bili_jct=body.bili_jct,
            buvid3=body.buvid3,
        )
    except Exception:
        # Same rationale as the QR-poll path: a state-refresh blip
        # must not turn a successful login into a 5xx — the .env is
        # already on disk. Log and move on.
        logger.exception(
            "auth_routes.manual_cookies: post-success state refresh failed"
        )

    return ManualCookiesResponse(
        uname=cast(str, result["uname"]),
        mid=cast(int, result["mid"]),
    )


__all__ = [
    "AuthStatusResponse",
    "BootstrapResponse",
    "ManualCookiesRequest",
    "ManualCookiesResponse",
    "QrPollResponse",
    "QrStartResponse",
    "get_http_client",
    "router",
]
