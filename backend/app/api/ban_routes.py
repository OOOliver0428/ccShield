"""T18 — Ban REST routes + WS banlist bridge.

Mounted under ``/api`` via :data:`app.api.router.api_router`. Endpoints:

* ``POST   /api/ban``                {room_id, uid, hour, reason?}
    → :meth:`BilibiliClient.ban_user`; on success update the live
    :class:`BanListManager` (``on_ban``) when one is running for the
    same room; return ``{ok: true}``.
* ``DELETE /api/ban``                {room_id, block_id, uid}
    → :meth:`BilibiliClient.unban_user`; on success call
    :meth:`BanListManager.on_unban`; return ``{ok: true}``.
* ``GET    /api/ban-list/{room_id}`` → current ban list. Read from the
    running manager's local state when available, else fall back to
    :meth:`BilibiliClient.get_ban_list`.
* ``WS     /api/ws/rooms/{room_id}/banlist``
    → stream :class:`BanListMessage` frames. On connect the route
    lazily creates (or re-uses) the manager singleton and starts it for
    the requested room; on disconnect the push callback is
    unsubscribed. The ``?token=`` query-string fallback is honored by
    :class:`app.api.middleware.LocalTokenMiddleware` so browser
    WebSocket clients work without custom headers.

Error mapping (POST/DELETE ``/ban``):

- :class:`AuthExpiredError`      → 401 + ``auth_session.handle_auth_expired()``
- :class:`PermissionDeniedError` → 403
- :class:`RateLimitedError`      → 429
- :class:`BiliApiError` (other)  → 502
- ``ban_user`` / ``unban_user`` returning ``False`` → 400

Test seams (mocked in ``tests/test_ban_routes.py``):

- ``_get_bili_client`` — module-level lazy factory (T13 precedent);
  tests monkeypatch the function so the route uses an ``AsyncMock``.
- ``banlist_manager`` — module-level singleton binding; tests
  monkeypatch the attribute so the route sees a fake.
- :data:`app.auth.session.auth_session` — accessed by name on the
  module so tests can swap it.

Strict typing: every parameter / return value carries an explicit type.
``Any`` is not used in the public surface; ``dict[str, object]`` is
preferred where the banlist wire format is heterogeneous.
"""
from __future__ import annotations

import time
from typing import Literal, cast

from fastapi import (
    APIRouter,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from app.auth import session as auth_session_module
from app.bilibili.client import BilibiliClient
from app.bilibili.exceptions import (
    AuthExpiredError,
    BiliApiError,
    PermissionDeniedError,
    RateLimitedError,
)
from app.room.banlist import (
    BanEntry,
    BanListManager,
    BanListMessage,
    BanTimestamp,
    normalize_ban_entry,
    set_banlist_manager,
)

router: APIRouter = APIRouter(tags=["ban"])


# ---------------------------------------------------------------------------
# Module-level seams (mocked in tests)
# ---------------------------------------------------------------------------

# Lazy ``BilibiliClient`` singleton. Tests patch ``_get_bili_client``
# to return an ``AsyncMock`` so the route never touches the network.
_bili_client: BilibiliClient | None = None


def _get_bili_client() -> BilibiliClient:
    """Return the process-wide :class:`BilibiliClient` (lazy singleton).

    The module stays importable before any auth / config is wired — a
    test or a misconfigured prod can override ``_get_bili_client`` on
    this module without paying the cost of a real ``httpx.AsyncClient``.
    """
    global _bili_client
    if _bili_client is None:
        _bili_client = BilibiliClient()
    # QR/manual login mutates settings after this singleton may already have
    # been constructed. Refresh both the cookie jar and the cached CSRF token
    # before every moderation read/write.
    from app.config import settings

    _bili_client.update_cookies(dict(settings.cookies))
    return _bili_client


# Module-level singleton handle for the ban-list manager. ``None`` until
# the first WS connect lazily creates one. Tests monkeypatch this
# attribute to inject a fake ``BanListManager``-shaped object.
banlist_manager: BanListManager | None = None


# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------


class BanRequest(BaseModel):
    """Request body for ``POST /api/ban``.

    ``extra="forbid"`` so a typo'd field (e.g. ``hours``) is rejected
    with 422 instead of silently ignored — moderator actions must not
    silently drop a parameter the operator thinks they sent.
    """

    model_config = ConfigDict(extra="forbid")

    room_id: int = Field(gt=0)
    uid: int = Field(gt=0)
    hour: Literal[-1, 0, 2, 4, 24, 168]
    reason: str = Field(default="", max_length=200)
    uname: str = Field(default="", max_length=100)


class UnbanRequest(BaseModel):
    """Request body for ``DELETE /api/ban``."""

    model_config = ConfigDict(extra="forbid")

    room_id: int = Field(gt=0)
    block_id: int = Field(gt=0)
    uid: int = Field(gt=0)


class BanEntryResponse(BaseModel):
    """Stable public shape for one B站 silent-user record."""

    model_config = ConfigDict(extra="forbid")

    block_id: int | None
    uid: int
    uname: str
    operator_uid: int | None
    operator_name: str
    hour: int | None
    reason: str
    created_at: BanTimestamp
    expires_at: BanTimestamp
    pending: bool


class BanListResponse(BaseModel):
    """Response body for ``GET /api/ban-list/{room_id}``.

    Raw B站 aliases are normalized by :mod:`app.room.banlist` before
    they cross this boundary, so every browser receives the same typed
    fields and can safely gate 解禁 on ``block_id`` / ``pending``.
    """

    model_config = ConfigDict(extra="ignore")

    room_id: int
    bans: list[BanEntryResponse]


class OkResponse(BaseModel):
    """Response body for ``POST`` / ``DELETE /api/ban`` on success."""

    model_config = ConfigDict(extra="forbid")

    ok: bool = True


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _http_for_bili_error(exc: BiliApiError) -> HTTPException:
    """Translate a :class:`BiliApiError` subclass to an :class:`HTTPException`.

    ``AuthExpiredError`` deliberately does NOT call
    ``handle_auth_expired`` here — that's an ``async`` side-effect and
    must run on the request task. The caller (route handler) is the
    right place for it. The returned exception's ``status_code`` is the
    integer FastAPI expects.
    """
    if isinstance(exc, AuthExpiredError):
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"auth expired: {exc.message}",
        )
    if isinstance(exc, PermissionDeniedError):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"permission denied: {exc.message}",
        )
    if isinstance(exc, RateLimitedError):
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"rate limited: {exc.message}",
        )
    # Other BiliApiError codes (network/auth/throttle we don't model).
    logger.error("ban_routes: bili api error: {!r}", exc)
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"bilibili api error: {exc.message}",
    )


# ---------------------------------------------------------------------------
# REST routes
# ---------------------------------------------------------------------------


@router.post(
    "/ban",
    response_model=OkResponse,
    summary="Ban a user in the given room",
)
async def post_ban_route(body: BanRequest) -> OkResponse:
    """Ban ``body.uid`` in ``body.room_id`` for ``body.hour`` hours.

    Requires an authenticated session. On success, updates the live
    :class:`BanListManager` if it is currently running for the same
    room so every connected WS client receives a ``ban_added`` delta.
    """
    auth_session_module.auth_session.require_authenticated()
    bili: BilibiliClient = _get_bili_client()
    try:
        ok: bool = await bili.ban_user(
            body.room_id, body.uid, body.hour, body.reason
        )
    except BiliApiError as exc:
        if isinstance(exc, AuthExpiredError):
            await auth_session_module.auth_session.handle_auth_expired()
        raise _http_for_bili_error(exc) from exc

    if not ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ban failed",
        )

    # Push to the running banlist manager (if any) so the WS clients
    # see the delta without waiting for the next reconcile tick.
    if (
        banlist_manager is not None
        and banlist_manager._room_id == body.room_id
    ):
        created_at = int(time.time())
        pending_entry = normalize_ban_entry(
            {
                "uid": body.uid,
                "uname": body.uname,
                "hour": body.hour,
                "reason": body.reason,
                "created_at": created_at,
            },
            pending=True,
        )
        assert pending_entry is not None
        await banlist_manager.on_ban(
            body.uid,
            pending_entry,
        )
        # Best-effort immediate reconciliation supplies the B站 block id.
        # The write already succeeded, so a transient read failure must not
        # turn the POST into a false failure; the pending row remains visible
        # and the periodic/manual refresh can complete it later.
        try:
            await banlist_manager.refresh()
        except AuthExpiredError as exc:
            await auth_session_module.auth_session.handle_auth_expired()
            logger.warning(
                "ban_routes: post-ban refresh found expired auth room={} "
                "uid={} err={!r}",
                body.room_id,
                body.uid,
                exc,
            )
        except Exception as exc:
            logger.warning(
                "ban_routes: post-ban list refresh failed room={} uid={} "
                "err={!r}",
                body.room_id,
                body.uid,
                exc,
            )

    return OkResponse(ok=True)


@router.delete(
    "/ban",
    response_model=OkResponse,
    summary="Unban a user by block id",
)
async def delete_ban_route(body: UnbanRequest) -> OkResponse:
    """Unban ``body.uid`` in ``body.room_id`` using ``body.block_id``.

    Requires an authenticated session. On success, broadcasts a
    ``ban_removed`` delta through the running :class:`BanListManager`.
    """
    auth_session_module.auth_session.require_authenticated()
    bili: BilibiliClient = _get_bili_client()
    try:
        ok: bool = await bili.unban_user(body.room_id, body.block_id)
    except BiliApiError as exc:
        if isinstance(exc, AuthExpiredError):
            await auth_session_module.auth_session.handle_auth_expired()
        raise _http_for_bili_error(exc) from exc

    if not ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="unban failed",
        )

    if (
        banlist_manager is not None
        and banlist_manager._room_id == body.room_id
    ):
        await banlist_manager.on_unban(body.uid)

    return OkResponse(ok=True)


@router.get(
    "/ban-list/{room_id}",
    response_model=BanListResponse,
    summary="Read the current ban list for a room",
)
async def get_ban_list_route(
    room_id: int, *, refresh: bool = False
) -> BanListResponse:
    """Return the ban list for ``room_id``.

    Reads from the running :class:`BanListManager`'s local state when
    it is currently started for ``room_id`` (the cheap path — no
    network round-trip). Falls back to
    :meth:`BilibiliClient.get_ban_list` otherwise (a fresh fetch
    paginates up to ``_BAN_LIST_MAX_PAGES``).
    """
    auth_session_module.auth_session.require_authenticated()

    try:
        if banlist_manager is not None and banlist_manager._room_id == room_id:
            if refresh:
                # The manager owns the same process-wide client in production.
                # Touch the factory before an upstream refresh so QR/manual
                # logins made after manager construction update its cookie jar
                # and CSRF token as well.
                _get_bili_client()
                entries: list[BanEntry] = await banlist_manager.refresh()
            else:
                entries = []
                for cached in banlist_manager._bans.values():
                    normalized = normalize_ban_entry(
                        cast("dict[str, object]", cached),
                        pending=cached.get("pending") is True,
                    )
                    if normalized is not None:
                        entries.append(normalized)
        else:
            bili: BilibiliClient = _get_bili_client()
            raw_entries = await bili.get_ban_list(room_id)
            entries = []
            for raw in raw_entries:
                if not isinstance(raw, dict):
                    continue
                normalized = normalize_ban_entry(
                    cast("dict[str, object]", raw)
                )
                if normalized is not None:
                    entries.append(normalized)
    except BiliApiError as exc:
        if isinstance(exc, AuthExpiredError):
            await auth_session_module.auth_session.handle_auth_expired()
        raise _http_for_bili_error(exc) from exc

    return BanListResponse(
        room_id=room_id,
        bans=[BanEntryResponse.model_validate(entry) for entry in entries],
    )


async def stop_banlist_manager() -> None:
    """Stop and clear the active single-room ban-list manager."""
    global banlist_manager
    manager = banlist_manager
    banlist_manager = None
    set_banlist_manager(None)
    if manager is not None:
        await manager.stop()


# ---------------------------------------------------------------------------
# WebSocket route
# ---------------------------------------------------------------------------


@router.websocket("/ws/rooms/{room_id}/banlist")
async def ws_banlist_route(websocket: WebSocket, room_id: int) -> None:
    """Stream ban-list push events to a connected WS client.

    Lifecycle:

    1. Accept the connection (the middleware has already verified
       ``?token=<LOCAL_TOKEN>`` and the ``Host`` guard).
    2. Lazily create the :class:`BanListManager` singleton if it
       doesn't exist yet, and start it for ``room_id`` (T17's
       :meth:`BanListManager.start` is idempotent on the same room).
    3. Register a push callback that forwards every
       :class:`BanListMessage` as JSON. The callback swallows send
       errors so a slow client cannot poison the broadcast loop.
    4. Loop receiving text frames — we ignore inbound messages today;
       the loop exists only to keep the OS-level keepalive + ping/pong
       alive so the connection doesn't time out.
    5. On disconnect, unsubscribe the callback so the manager's
       subscriber list doesn't accumulate stale closures.

    ``banlist_manager.stop()`` is intentionally NOT called on
    disconnect — a freshly-disconnected WS may have peers about to
    reconnect, and ``stop`` would also tear down the reconcile task.
    A future task can introduce an idle-timeout if needed.
    """
    await websocket.accept()

    # Local imports keep the top of the module a clean dependency
    # graph; ``get_room_bridge`` is used by the ``is_running`` callback
    # so the reconcile task short-circuits when the room goes away.
    from app.room.bridge import get_room_bridge

    global banlist_manager
    manager: BanListManager | None = banlist_manager
    if manager is None:
        manager = BanListManager(_get_bili_client())
        banlist_manager = manager
        # Mirror the write into the canonical T17 singleton so the
        # other side (``app.room.banlist.banlist_manager``) sees it.
        set_banlist_manager(manager)

    # Start (or restart) for the requested room. T17's start() cancels
    # a prior reconcile task and re-fetches the snapshot when the room
    # changes, so this is safe to call on every connect.
    if manager._room_id != room_id:
        await manager.start(
            room_id,
            is_running=lambda: get_room_bridge() is not None,
        )

    async def _push(msg: BanListMessage) -> None:
        """Forward ``msg`` to the WS. Send errors are logged and swallowed."""
        try:
            await websocket.send_json(dict(msg))
        except Exception as exc:
            logger.warning(
                "ban_routes.ws_banlist_route: send failed ({!r}); "
                "dropping frame",
                exc,
            )

    await manager.subscribe(_push)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.unsubscribe(_push)


__all__ = [
    "BanEntryResponse",
    "BanListResponse",
    "BanRequest",
    "OkResponse",
    "UnbanRequest",
    "_get_bili_client",
    "banlist_manager",
    "router",
    "stop_banlist_manager",
]
