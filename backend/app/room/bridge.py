"""WebSocket bridge between a :class:`RoomSession` and zero+ clients (T13).

The bridge sits BETWEEN the T12 :class:`RoomSession` (a normalised
event source) and the FastAPI WebSocket route. The session emits typed
:class:`BridgeEvent` values; the bridge fans them out as JSON to
every registered client while keeping a short replay buffer so a
newly-connected UI can rehydrate its view from the last few events.

Design contract:

* **Snapshot on connect.** ``register_ws`` sends the most recent
  ``_MAX_SNAPSHOT`` (50) events as JSON so a freshly-opened websocket
  catches up with what's already happened.
* **Forward only ``model_dump()`` shape.** No raw B站 ``cmd``/``info``
  dicts ever cross this boundary — the session's
  :meth:`RoomSession._normalize` is the only place that touches raw
  payloads.
* **Defensive fan-out.** ``_on_event`` catches + logs every
  per-client ``send_json`` exception and drops that client from the
  registry, so a single broken peer can never block the others.
* **Single bridge per process.** Matches T12's single-room invariant.
  The module-level ``room_bridge`` singleton + ``get_room_bridge`` /
  ``set_room_bridge`` helpers hold the currently-active bridge; the
  FastAPI route swaps it on ``/rooms/start`` and clears it on
  ``/rooms/stop``.

Construction is async (``RoomBridge.create``) because
:meth:`RoomSession.add_callback` acquires an asyncio lock — it cannot
be called from inside a synchronous ``__init__``.
"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING, Final, Literal

from loguru import logger
from starlette.websockets import WebSocket

from app.room.events import BridgeEvent, RoomStatusEvent

if TYPE_CHECKING:
    from app.room.session import RoomSession

#: Maximum events retained in the replay buffer. Sized to comfortably
#: outlive a typical reconnect burst; bounded so a chatty room cannot
#: grow memory without bound.
_MAX_HISTORY: Final[int] = 100

#: Number of recent events replayed on a fresh ``register_ws``. Caps the
#: bandwidth of a single connect at the price of missing some older
#: events — UI catches up via the live stream.
_MAX_SNAPSHOT: Final[int] = 50

#: Literal alias matching ``RoomStatusEvent.status``. Re-exposed here so
#: the helper signature is checkable without importing the events module
#: at type-check time only.
StatusLiteral = Literal["connected", "disconnected", "reconnecting", "error"]


class RoomBridge:
    """Fan-out bridge between a single :class:`RoomSession` and WS clients.

    See module docstring for the full contract.
    """

    def __init__(self, room_session: RoomSession) -> None:
        self._room_session: RoomSession = room_session
        self._history: deque[BridgeEvent] = deque(maxlen=_MAX_HISTORY)
        self._clients: set[WebSocket] = set()
        # Serialises add/remove and snapshots the broadcast target list,
        # mirroring the T12 broadcast loop's concurrency story.
        self._register_lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    async def create(cls, room_session: RoomSession) -> RoomBridge:
        """Async factory: builds the bridge and registers the event callback.

        ``RoomSession.add_callback`` is ``async`` because it acquires a
        lock — it cannot be awaited from inside ``__init__``. Going via a
        classmethod keeps callers (the FastAPI routes) declarative:

            bridge = await RoomBridge.create(session)
            set_room_bridge(bridge)

        The returned bridge is ready to accept WS clients.
        """
        bridge = cls(room_session)
        await room_session.add_callback(bridge._on_event)
        return bridge

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def room_session(self) -> RoomSession:
        """The :class:`RoomSession` this bridge subscribes to."""
        return self._room_session

    @property
    def history_snapshot(self) -> list[BridgeEvent]:
        """Return the most recent events (capped at ``_MAX_SNAPSHOT``)."""
        # ``list(self._history)[-_MAX_SNAPSHOT:]`` would call dunder
        # ``__getitem__`` with a slice on a deque, which a deque handles
        # natively. The result is a fresh list, so the caller cannot
        # mutate the buffer by accident.
        return list(self._history)[-_MAX_SNAPSHOT:]

    @property
    def client_count(self) -> int:
        """Current number of registered WS clients (test/diagnostic)."""
        return len(self._clients)

    # ------------------------------------------------------------------
    # Client registration
    # ------------------------------------------------------------------

    async def register_ws(self, ws: WebSocket) -> None:
        """Add ``ws`` to the fan-out set and replay recent history.

        The history replay is a fire-and-best-effort: a ``send_json``
        failure during the replay removes the socket from the registry
        (it was already dead) and bails out of the snapshot loop without
        raising — the live broadcast path will see the same thing.
        """
        async with self._register_lock:
            self._clients.add(ws)

        snapshot: list[BridgeEvent] = self.history_snapshot
        for event in snapshot:
            try:
                await ws.send_json(event.model_dump())
            except Exception as exc:
                logger.warning(
                    "RoomBridge.register_ws: snapshot send failed ({!r}); "
                    "dropping ws",
                    exc,
                )
                await self.unregister_ws(ws)
                return

    async def unregister_ws(self, ws: WebSocket) -> None:
        """Remove ``ws`` from the fan-out set. No-op if not registered."""
        async with self._register_lock:
            self._clients.discard(ws)

    # ------------------------------------------------------------------
    # RoomSession callback — invoked for every normalised event
    # ------------------------------------------------------------------

    async def _on_event(self, event: BridgeEvent) -> None:
        """Append to the buffer, then broadcast ``model_dump()`` to all WS.

        Every per-client send is wrapped in a try/except so a single
        broken peer cannot starve the others. Failures are logged and
        the offending socket is dropped from the registry so we do not
        keep paying the cost of writing to it.
        """
        # 1. Append to history. Bounded by maxlen so this cannot OOM.
        self._history.append(event)

        # 2. Snapshot the clients under the lock so a concurrent
        #    register/unregister doesn't race the broadcast loop.
        async with self._register_lock:
            targets: set[WebSocket] = set(self._clients)

        # 3. Fan out. ``asyncio.gather`` parallelises the writes; a
        #    failure on one peer does not block the others.
        if not targets:
            logger.debug(
                "RoomBridge._on_event: no ws clients registered room={} "
                "type={}",
                self._room_session.room_id,
                event.type,
            )
            return

        logger.debug(
            "RoomBridge._on_event: broadcasting type={} room={} clients={}",
            event.type,
            self._room_session.room_id,
            len(targets),
        )

        payload: dict[str, object] = event.model_dump()

        async def _send(target: WebSocket) -> WebSocket | None:
            try:
                await target.send_json(payload)
                return None
            except Exception as exc:
                logger.error(
                    "RoomBridge._on_event: send_json failed: {!r}", exc
                )
                return target

        results: list[WebSocket | None] = await asyncio.gather(
            *(_send(t) for t in targets)
        )
        dead: list[WebSocket] = [t for t in results if t is not None]
        if dead:
            logger.warning(
                "RoomBridge._on_event: dropping {} dead ws clients",
                len(dead),
            )
            async with self._register_lock:
                for ws in dead:
                    self._clients.discard(ws)

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    async def broadcast_status(self, status: StatusLiteral) -> None:
        """Push a :class:`RoomStatusEvent` to the bridge's own fan-out.

        Cheap, in-process way to surface a state change (e.g. an
        authentication expiry, a manual pause) to every connected UI
        without round-tripping through the B站 WS.
        """
        event = RoomStatusEvent(type="room_status", status=status)
        await self._on_event(event)


# ---------------------------------------------------------------------------
# Module-level singleton — single active RoomBridge per process.
# ---------------------------------------------------------------------------


#: The currently-active bridge, or ``None`` when no room has been started.
#: Plain attribute on the module so production code mutates it via
#: ``set_room_bridge(...)`` and reads it via ``get_room_bridge()``.
room_bridge: RoomBridge | None = None


def get_room_bridge() -> RoomBridge | None:
    """Return the current :class:`RoomBridge`, or ``None`` when stopped."""
    return room_bridge


def set_room_bridge(bridge: RoomBridge | None) -> None:
    """Install / clear the active :class:`RoomBridge` singleton."""
    global room_bridge
    room_bridge = bridge


__all__ = [
    "RoomBridge",
    "get_room_bridge",
    "room_bridge",
    "set_room_bridge",
]
