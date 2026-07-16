"""T13 — Room REST routes + normalised WebSocket bridge endpoint.

Mounted under ``/api`` via ``app.api.router``. Endpoints:

* ``GET  /api/rooms/resolve?input=<int>`` — translate a user-supplied
  id (real or short) to a B站 canonical record via
  :meth:`BilibiliClient.resolve_room_id`.
* ``POST /api/rooms/start`` {room_id} — create a ``RoomSession`` via
  :func:`_make_room_session`, connect, then install a new
  :class:`RoomBridge` as the active singleton. 400 on connect failure.
* ``POST /api/rooms/stop`` — disconnect the active session and clear
  the singleton. Idempotent (no-op when no bridge is installed).
* ``GET  /api/rooms`` — read-only view of the current bridge's room_id
  + status.
* ``WS   /api/ws/rooms/{room_id}`` — accept; if no active bridge for
  that room id, send ``{type:"error",...}`` and close. Otherwise replay
  the recent history then stream every normalised ``BridgeEvent`` as
  ``event.model_dump()``.

Test seams (mocked by name in ``tests/test_room_routes.py``):

* ``_get_bili_client`` — production lazy-singleton; tests return a mock.
* ``_make_room_session`` — production returns ``RoomSession(bili)``;
  tests return a mock with ``connect``/``disconnect`` AsyncMocks.
* :func:`set_room_bridge` and :func:`get_room_bridge` — bridge singleton.

Why ``/api/ws/*`` and not bare ``/ws/*``: the ``LocalTokenMiddleware``
guards ``/api/*`` and ``/ws/*``. Mounting this router under
``/api`` means the WS route's actual URL is ``/api/ws/rooms/{room_id}``,
which the middleware still protects (Bearer path prefix is ``/api``).
The ``?token=`` query-string fallback only fires for paths that
literally start with ``/ws``; we send ``Authorization: Bearer`` on the
WS endpoint accordingly.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import (
    APIRouter,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from app.bilibili.client import BilibiliClient, RoomUserRole
from app.room.bridge import (
    RoomBridge,
    get_room_bridge,
    set_room_bridge,
)
from app.room.session import RoomSession

router: APIRouter = APIRouter(tags=["rooms"])
_ROLE_LOOKUP_TIMEOUT_SECONDS = 5.0


# ---------------------------------------------------------------------------
# Module-level seams (mocked in tests)
# ---------------------------------------------------------------------------


_bili_client: BilibiliClient | None = None


def _get_bili_client() -> BilibiliClient:
    """Lazy singleton :class:`BilibiliClient`.

    The module stays importable before auth is configured — a test or
    a misconfigured prod can override ``_get_bili_client`` on this
    module without paying the cost of a real ``httpx.AsyncClient``.

    The singleton's httpx cookie jar is refreshed from
    :data:`app.config.settings` on EVERY access, not just at first
    construction. The client may have been built when ``.env`` was
    empty (cold start) and a subsequent QR / manual login mutated
    ``settings`` in place; without the refresh, downstream B站 calls
    (e.g. ``resolve_room_id``) would carry an empty jar and fail.
    """
    global _bili_client
    if _bili_client is None:
        _bili_client = BilibiliClient()
    # Push the latest settings into the jar (idempotent dict upsert).
    from app.config import settings

    _bili_client.update_cookies(dict(settings.cookies))
    return _bili_client


async def _make_room_session(bili_client: BilibiliClient) -> RoomSession:
    """Construct a :class:`RoomSession` wrapping ``bili_client``.

    Production default; tests replace this attribute on the module to
    return a mock so the rest of the route runs without a real Bili WS.
    """
    return RoomSession(bili_client)


# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------


class ResolveRoomResponse(BaseModel):
    """Response body for ``GET /api/rooms/resolve``.

    ``extra="ignore"`` because B站's payload can carry extra fields that
    are useful for the UI (description, area_id, ...) but we only promise
    the typed surface here.
    """

    model_config = ConfigDict(extra="ignore")

    room_id: int
    short_id: int = 0
    uid: int | None = None
    title: str = ""
    uname: str = ""
    live_status: int = 0
    is_short_id: bool = False


class StartRoomRequest(BaseModel):
    """Request body for ``POST /api/rooms/start``."""

    model_config = ConfigDict(extra="forbid")

    room_id: int = Field(gt=0, description="Real or short room id")


class StartRoomResponse(BaseModel):
    """Response body for ``POST /api/rooms/start`` on success."""

    model_config = ConfigDict(extra="ignore")

    room_id: int
    title: str = ""
    role: RoomUserRole = "unknown"


class StopRoomResponse(BaseModel):
    """Response body for ``POST /api/rooms/stop``."""

    model_config = ConfigDict(extra="forbid")

    ok: bool = True


class RoomStatusResponse(BaseModel):
    """Response body for ``GET /api/rooms``."""

    model_config = ConfigDict(extra="forbid")

    current: int | None
    status: str


# ---------------------------------------------------------------------------
# REST routes
# ---------------------------------------------------------------------------


@router.get(
    "/rooms/resolve",
    response_model=ResolveRoomResponse,
    summary="Resolve a B站 room short id or real id to canonical info",
)
async def resolve_room_route(input: int) -> ResolveRoomResponse:
    """Translate ``input`` (real or short id) via B站 HTTP client.

    Returns 404 when :meth:`BilibiliClient.resolve_room_id` cannot
    resolve the id (B站 returned a payload without a ``room_id``).
    """
    bili: BilibiliClient = _get_bili_client()
    info: dict[str, Any] | None = await bili.resolve_room_id(input)
    if not isinstance(info, dict) or "room_id" not in info:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="room not found",
        )
    return ResolveRoomResponse(
        room_id=int(info["room_id"]),
        short_id=int(info.get("short_id", input) or input),
        uid=(int(info["uid"]) if isinstance(info.get("uid"), int) else None),
        title=str(info.get("title", "") or ""),
        uname=str(info.get("uname", "") or ""),
        live_status=int(info.get("live_status", 0) or 0),
        is_short_id=bool(info.get("is_short_id", False)),
    )


@router.post(
    "/rooms/start",
    response_model=StartRoomResponse,
    summary="Connect to a live room",
)
async def start_room_route(body: StartRoomRequest) -> StartRoomResponse:
    """Build a :class:`RoomSession`, connect, install a bridge singleton.

    Failure path: 400 if ``session.connect`` returns ``False``. The
    singleton bridge is NOT installed in that case — a failed start
    must not leave a half-wired bridge behind.
    """
    # Ban-list reconciliation is scoped to the active room. Stop it before
    # replacing the room bridge so no stale task can fetch or push entries
    # from the previous room during the switch.
    from app.api.ban_routes import stop_banlist_manager

    await stop_banlist_manager()

    previous = get_room_bridge()
    if previous is not None:
        try:
            await previous.room_session.disconnect()
        except Exception as exc:
            logger.exception(
                "room_routes.start_room_route: previous disconnect failed: {!r}",
                exc,
            )
        finally:
            await previous.close()
            set_room_bridge(None)

    bili: BilibiliClient = _get_bili_client()
    session: RoomSession = await _make_room_session(bili)

    ok: bool = await session.connect(body.room_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="room connect failed",
        )

    bridge: RoomBridge = await RoomBridge.create(session)
    set_room_bridge(bridge)
    # RoomSession resolves short ids before it constructs the Bilibili WS.
    # Return that canonical id so the browser connects to the same bridge.
    resolved_room_id = getattr(session, "room_id", None)
    connected_room_id = (
        resolved_room_id if isinstance(resolved_room_id, int) else body.room_id
    )
    try:
        role = await asyncio.wait_for(
            bili.get_room_user_role(connected_room_id),
            timeout=_ROLE_LOOKUP_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        # Role display is useful context, not a prerequisite for receiving
        # danmaku. A transient read failure must not tear down a live bridge.
        logger.warning(
            "room_routes.start_room_route: role lookup failed room={} err={!r}",
            connected_room_id,
            exc,
        )
        role = "unknown"
    return StartRoomResponse(room_id=connected_room_id, title="", role=role)


@router.post(
    "/rooms/stop",
    response_model=StopRoomResponse,
    summary="Disconnect the live-room bridge",
)
async def stop_room_route() -> StopRoomResponse:
    """Disconnect the active session and clear the bridge singleton.

    Idempotent: with no active bridge the call is a no-op and still
    returns ``{ok: true}``.
    """
    from app.api.ban_routes import stop_banlist_manager

    bridge: RoomBridge | None = get_room_bridge()
    if bridge is not None:
        try:
            await bridge.room_session.disconnect()
        except Exception as exc:
            # A failure during disconnect must not leave a stale bridge
            # behind — the client would receive no further events but the
            # room_session would still hold a live client.
            logger.exception(
                "room_routes.stop_room_route: disconnect failed: {!r}", exc
            )
        finally:
            await bridge.close()
            set_room_bridge(None)
    await stop_banlist_manager()
    return StopRoomResponse(ok=True)


@router.get(
    "/rooms",
    response_model=RoomStatusResponse,
    summary="Read the current room + bridge state",
)
async def get_room_route() -> RoomStatusResponse:
    """Return the active room_id + status (``None``/``disconnected`` when idle)."""
    bridge: RoomBridge | None = get_room_bridge()
    if bridge is None:
        return RoomStatusResponse(current=None, status="disconnected")
    return RoomStatusResponse(
        current=bridge.room_session.room_id,
        status=bridge.room_session.status,
    )


# ---------------------------------------------------------------------------
# WebSocket route
# ---------------------------------------------------------------------------


@router.websocket("/ws/rooms/{room_id}")
async def ws_rooms_route(websocket: WebSocket, room_id: int) -> None:
    """WS endpoint that streams ``BridgeEvent.model_dump()`` to the UI.

    Behaviour:

    * Accept the connection.
    * If no bridge is active, or the bridge is on a different ``room_id``,
      send ``{"type":"error","message":"房间未启动"}`` and close. The
      frontend reads that, tells the user to start a room, and stops
      trying to reconnect.
    * Otherwise, register the WS with the bridge (replays the last
      ``MAX_SNAPSHOT`` events), then loop reading (ignore commands —
      the UI doesn't send any today, but we keep the socket open so the
      OS-level keepalive + ping/pong behave normally). On disconnect,
      unregister the WS.

    ``room_id`` is path-validated by FastAPI's int coercion; a non-int
    path produces a 422 before this handler runs.
    """
    await websocket.accept()

    bridge: RoomBridge | None = get_room_bridge()
    if bridge is None or bridge.room_session.room_id != room_id:
        await websocket.send_json({"type": "error", "message": "房间未启动"})
        await websocket.close()
        return

    await bridge.register_ws(websocket)
    try:
        while True:
            # Drain the socket; we don't act on messages today.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await bridge.unregister_ws(websocket)


__all__ = [
    "ResolveRoomResponse",
    "RoomStatusResponse",
    "StartRoomRequest",
    "StartRoomResponse",
    "StopRoomResponse",
    "_get_bili_client",
    "_make_room_session",
    "router",
]
