"""WebSocket bridge between a :class:`RoomSession` and zero+ clients (T13).

The bridge sits BETWEEN the T12 :class:`RoomSession` (a normalised
event source) and the FastAPI WebSocket route. The session emits typed
:class:`BridgeEvent` values; the bridge fans them out as JSON to
every registered client while keeping a short replay buffer so a
newly-connected UI can rehydrate its view from the last few events.

Design contract:

* **Snapshot on connect.** ``register_ws`` queues the most recent
  ``_MAX_SNAPSHOT`` (50) events so a freshly-opened websocket catches up.
* **Forward only ``model_dump()`` shape.** No raw B站 ``cmd``/``info``
  dicts ever cross this boundary — the session's
  :meth:`RoomSession._normalize` is the only place that touches raw
  payloads.
* **Per-client isolation.** Every browser owns an independent bounded send
  queue and sender task. ``_on_event`` only performs ``put_nowait``; a slow
  browser is disconnected when its queue fills and can never backpressure
  the B站 receive loop.
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
import contextlib
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from loguru import logger
from starlette.websockets import WebSocket

from app.room.events import (
    BridgeEvent,
    RoomStatusEvent,
    SuperChatDeleteEvent,
    SuperChatEvent,
)

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
_CLIENT_QUEUE_MAXSIZE: Final[int] = 2_000
_CLIENT_SEND_TIMEOUT: Final[float] = 2.0
_CLIENT_CLOSE_TIMEOUT: Final[float] = 1.0

#: Literal alias matching ``RoomStatusEvent.status``. Re-exposed here so
#: the helper signature is checkable without importing the events module
#: at type-check time only.
StatusLiteral = Literal["connected", "disconnected", "reconnecting", "error"]


@dataclass(slots=True)
class _BridgeMetrics:
    events_received: int = 0
    deliveries_enqueued: int = 0
    deliveries_sent: int = 0
    send_failures: int = 0
    slow_clients_dropped: int = 0
    client_queue_peak: int = 0


class RoomBridge:
    """Fan-out bridge between a single :class:`RoomSession` and WS clients.

    See module docstring for the full contract.
    """

    def __init__(
        self,
        room_session: RoomSession,
        *,
        _client_queue_maxsize: int = _CLIENT_QUEUE_MAXSIZE,
        _send_timeout: float = _CLIENT_SEND_TIMEOUT,
    ) -> None:
        if _client_queue_maxsize <= 0:
            raise ValueError("client queue maxsize must be positive")
        self._room_session: RoomSession = room_session
        self._history: deque[BridgeEvent] = deque(maxlen=_MAX_HISTORY)
        initial_super_chats = getattr(room_session, "initial_super_chats", [])
        self._active_super_chats: dict[str, SuperChatEvent] = {
            event.id: event
            for event in initial_super_chats
            if isinstance(event, SuperChatEvent)
            and event.id
            and event.end_ts > int(time.time())
        }
        self._clients: set[WebSocket] = set()
        self._client_queues: dict[
            WebSocket, asyncio.Queue[dict[str, object]]
        ] = {}
        self._sender_tasks: dict[WebSocket, asyncio.Task[None]] = {}
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._client_queue_maxsize = _client_queue_maxsize
        self._send_timeout = _send_timeout
        self._closed = False
        self._metrics = _BridgeMetrics()
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

    @property
    def metrics_snapshot(self) -> dict[str, int]:
        """Diagnostic counters for slow-client and fan-out pressure."""
        return {
            "events_received": self._metrics.events_received,
            "deliveries_enqueued": self._metrics.deliveries_enqueued,
            "deliveries_sent": self._metrics.deliveries_sent,
            "send_failures": self._metrics.send_failures,
            "slow_clients_dropped": self._metrics.slow_clients_dropped,
            "client_queue_peak": self._metrics.client_queue_peak,
            "client_count": self.client_count,
        }

    @property
    def active_super_chats(self) -> list[SuperChatEvent]:
        """Return unexpired paid messages independently of chat history."""
        self._prune_expired_super_chats()
        return list(self._active_super_chats.values())

    def _replay_snapshot(self) -> list[BridgeEvent]:
        """Replay recent chat plus active SCs evicted from normal history."""
        history = self.history_snapshot
        history_sc_ids = {
            event.id
            for event in history
            if isinstance(event, SuperChatEvent) and event.id
        }
        pinned = [
            event
            for event in self.active_super_chats
            if event.id not in history_sc_ids
        ]
        return [*pinned, *history]

    def _prune_expired_super_chats(self) -> None:
        now = int(time.time())
        expired = [
            sc_id
            for sc_id, event in self._active_super_chats.items()
            if event.end_ts <= now
        ]
        for sc_id in expired:
            self._active_super_chats.pop(sc_id, None)

    # ------------------------------------------------------------------
    # Client registration
    # ------------------------------------------------------------------

    async def register_ws(self, ws: WebSocket) -> None:
        """Add ``ws`` to the fan-out set and replay recent history.

        Snapshot and future live events share this client's private queue,
        preserving their order without blocking the upstream callback.
        """
        await self.unregister_ws(ws)
        async with self._register_lock:
            if self._closed:
                return
            # Snapshot history while holding the same lock used by
            # ``_on_event`` to append and enqueue. Otherwise an event can
            # land in history immediately before registration and then be
            # queued again as a live event for this client.
            snapshot = self._replay_snapshot()
            queue_size = max(self._client_queue_maxsize, len(snapshot) + 1)
            queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(
                maxsize=queue_size
            )
            for event in snapshot:
                queue.put_nowait(event.model_dump())
            self._clients.add(ws)
            self._client_queues[ws] = queue
            task = asyncio.create_task(
                self._client_sender(ws, queue),
                name=f"room_bridge_sender[{id(ws)}]",
            )
            self._sender_tasks[ws] = task
            self._metrics.deliveries_enqueued += len(snapshot)
            self._metrics.client_queue_peak = max(
                self._metrics.client_queue_peak,
                queue.qsize(),
            )

    async def unregister_ws(self, ws: WebSocket) -> None:
        """Remove ``ws`` from the fan-out set. No-op if not registered."""
        await self._detach_client(ws)

    async def _detach_client(self, ws: WebSocket) -> None:
        current = asyncio.current_task()
        async with self._register_lock:
            self._clients.discard(ws)
            self._client_queues.pop(ws, None)
            sender = self._sender_tasks.pop(ws, None)
        if sender is not None and sender is not current:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    async def _client_sender(
        self,
        ws: WebSocket,
        queue: asyncio.Queue[dict[str, object]],
    ) -> None:
        try:
            while True:
                payload = await queue.get()
                try:
                    await asyncio.wait_for(
                        ws.send_json(payload),
                        timeout=self._send_timeout,
                    )
                    self._metrics.deliveries_sent += 1
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self._metrics.send_failures += 1
            logger.warning(
                "RoomBridge: client sender failed ({!r}); dropping ws",
                exc,
            )
            await self._detach_client(ws)
            await self._close_ws(ws)

    async def _close_ws(self, ws: WebSocket) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                ws.close(),
                timeout=_CLIENT_CLOSE_TIMEOUT,
            )

    def _schedule_close(self, ws: WebSocket) -> None:
        task = asyncio.create_task(
            self._close_ws(ws),
            name=f"room_bridge_close[{id(ws)}]",
        )
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    # ------------------------------------------------------------------
    # RoomSession callback — invoked for every normalised event
    # ------------------------------------------------------------------

    async def _on_event(self, event: BridgeEvent) -> None:
        """Append to the buffer, then broadcast ``model_dump()`` to all WS.

        This method never performs network I/O. It updates replay state and
        enqueues one immutable payload into each client's private queue.
        """
        self._metrics.events_received += 1
        payload: dict[str, object] = event.model_dump()
        slow_clients: list[tuple[WebSocket, asyncio.Task[None] | None]] = []

        # State mutation, client snapshot replay and live enqueue share this
        # small critical section. That makes registration atomic with respect
        # to events: a new peer receives each event exactly once, either from
        # history or from its live queue.
        async with self._register_lock:
            # 1. Maintain a separate active-SC map. A two-hour SC must survive
            # far longer than the 50-event ordinary replay window.
            self._prune_expired_super_chats()
            if isinstance(event, SuperChatEvent) and event.id:
                if event.end_ts > int(time.time()):
                    self._active_super_chats[event.id] = event
            elif isinstance(event, SuperChatDeleteEvent):
                for sc_id in event.ids:
                    self._active_super_chats.pop(sc_id, None)

            # 2. Append to history. Bounded by maxlen so this cannot OOM.
            self._history.append(event)

            # 3. Queue independently for each client. A full client queue
            # means the peer cannot keep up; drop the peer, not upstream.
            for ws in tuple(self._clients):
                queue = self._client_queues.get(ws)
                if queue is None:
                    slow_clients.append((ws, self._sender_tasks.pop(ws, None)))
                    self._clients.discard(ws)
                    continue
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:
                    slow_clients.append((ws, self._sender_tasks.pop(ws, None)))
                    self._clients.discard(ws)
                    self._client_queues.pop(ws, None)
                    continue
                self._metrics.deliveries_enqueued += 1
                self._metrics.client_queue_peak = max(
                    self._metrics.client_queue_peak,
                    queue.qsize(),
                )

        if slow_clients:
            self._metrics.slow_clients_dropped += len(slow_clients)
            logger.warning(
                "RoomBridge: dropping {} slow clients room={} type={}",
                len(slow_clients),
                self._room_session.room_id,
                event.type,
            )
            for ws, sender in slow_clients:
                if sender is not None:
                    sender.cancel()
                self._schedule_close(ws)

    async def close(self) -> None:
        """Stop every sender task and detach from the RoomSession."""
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await self._room_session.remove_callback(self._on_event)

        async with self._register_lock:
            clients = list(self._clients)
            senders = list(self._sender_tasks.values())
            self._clients.clear()
            self._client_queues.clear()
            self._sender_tasks.clear()

        for sender in senders:
            sender.cancel()
        if senders:
            await asyncio.gather(*senders, return_exceptions=True)
        if clients:
            await asyncio.gather(
                *(self._close_ws(ws) for ws in clients),
                return_exceptions=True,
            )
        cleanup = list(self._cleanup_tasks)
        if cleanup:
            await asyncio.gather(*cleanup, return_exceptions=True)
        self._cleanup_tasks.clear()

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
