"""Single-room session with normalized event dispatch (T12).

``RoomSession`` wraps the (T11) :class:`DanmakuClient` for ONE room and
exposes a typed callback stream to the rest of the app. We deliberately
do NOT forward B站 raw cmd/info dicts — every event that hits a
callback has already been normalized into a :class:`BridgeEvent`.

Design decisions:

* **Single active room.** A second ``connect(room_id)`` automatically
  calls ``stop()`` on the previous client first. We never run two
  ``DanmakuClient`` instances in parallel here. Multi-room management
  is deferred to a higher layer if/when needed.
* **Dedup via ``dm_v2``.** B站 repeats DANMU_MSG across multiple
  server-side fan-outs; we keep a bounded ``deque`` of seen msg_ids and
  drop the second copy. Sized via the ``_dedup_size`` seam (default
  5000, matching ccShield's ``room_manager``).
* **Defensive parsing.** Anything malformed returns ``None`` from
  :meth:`_normalize`; we NEVER crash the broadcast loop on a bad dict.
* **Broadcast error isolation.** A single raising callback does NOT
  prevent the others from being invoked; errors are logged.
* **Lock around the callback list.** ``add_callback`` /
  ``remove_callback`` are async + serialized so a snapshot taken in
  :meth:`_broadcast` is consistent with the underlying state at that
  moment (subsequent changes do not affect the in-flight snapshot).

Tests mock :class:`DanmakuClient` at this module's import site
(``app.room.session.DanmakuClient``) using ``unittest.mock.patch`` —
no real network, fully deterministic.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Final, cast

from loguru import logger

from app.bilibili.danmaku_ws import DanmakuClient
from app.room.events import (
    BridgeEvent,
    DanmakuEvent,
    Medal,
    RoomStatusEvent,
    SuperChatEvent,
)

if TYPE_CHECKING:
    from app.bilibili.client import BilibiliClient


# Type alias for a BridgeEvent consumer. The annotation stays
# explicit (no Any) per the T12 spec.
BridgeCallback = Callable[[BridgeEvent], Awaitable[None]]

# Default dedup buffer size — matches ccShield's RoomManager.
DEFAULT_DEDUP_SIZE: Final[int] = 5000


class RoomSession:
    """ONE active room: a DanmakuClient + dedup + broadcast to callbacks.

    Single-active-room invariant: at most one ``DanmakuClient`` is alive
    at any time. A second ``connect()`` always stops the current client
    before constructing the new one.
    """

    def __init__(
        self,
        bili_client: BilibiliClient,
        *,
        _dedup_size: int = DEFAULT_DEDUP_SIZE,
    ) -> None:
        self.bili_client = bili_client
        self._dedup_size = _dedup_size

        # Owned client — None when not connected.
        self._client: DanmakuClient | None = None

        # Callback fan-out list (under a lock so add/remove and
        # broadcast snapshot are consistent).
        self._callbacks: list[BridgeCallback] = []
        self._callback_lock = asyncio.Lock()

        # Bounded seen-msg-id ring buffer. Reset on disconnect().
        self._seen_ids: deque[str] = deque(maxlen=_dedup_size)

        # Public, observable state.
        self.room_id: int | None = None
        self.status: str = "disconnected"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, room_id: int) -> bool:
        """Connect to ``room_id``. Single-active-room: stops any previous.

        The input may be a SHORT room id (URL-style) or the REAL id; we
        resolve via :meth:`BilibiliClient.get_room_init` before
        constructing the underlying ``DanmakuClient``. Without this
        step, B站 rejects the AUTH frame on real-room-id mismatch and
        no danmaku loads. If resolution fails (stub bili_client / no
        network) the input id is used as-is and a warning is logged.
        Defensive fallback keeps every existing unit test green.

        Returns ``True`` if ``DanmakuClient.start()`` reports a
        connected WS; ``False`` otherwise. The status flips to
        ``"connected"`` or ``"error"`` accordingly and a
        :class:`RoomStatusEvent` is broadcast to every registered
        callback.
        """
        logger.info("room_session.connect: input room_id={}", room_id)

        # Single-active-room invariant: stop any previous client.
        if self._client is not None:
            await self._client.stop()
            self._client = None

        # Resolve short→real room id. The B站 ``getDanmuInfo`` and AUTH
        # frames both require the real id; ccShield's proven RoomManager
        # does this via ``resolve_room_id``; here we call the lower-level
        # ``get_room_init`` so a network outage in this optional step
        # doesn't take down the WS connect.
        real_room_id = await self._resolve_room_id(room_id)

        client = DanmakuClient(
            real_room_id,
            self.bili_client,
            on_message=self._on_raw_message,
        )
        self._client = client

        ok = await client.start()
        if ok:
            self.room_id = real_room_id
            self.status = "connected"
            logger.info(
                "room_session.connect: connected room={} (input={})",
                real_room_id,
                room_id,
            )
            await self._broadcast(
                RoomStatusEvent(type="room_status", status="connected")
            )
            return True

        # start() failed (auth rejected, no danmu-info, no ws in
        # _auth_timeout window, etc.).
        self.room_id = None
        self._client = None
        self.status = "error"
        logger.error(
            "room_session.connect: start() returned False room={} input={}",
            real_room_id,
            room_id,
        )
        await self._broadcast(
            RoomStatusEvent(type="room_status", status="error")
        )
        return False

    async def _resolve_room_id(self, room_id: int) -> int:
        """Best-effort short→real translation via ``bili_client.get_room_init``.

        Returns the resolved real id when the bili client has the method
        and it returns a ``room_id``; otherwise returns ``room_id``
        unchanged. Never raises — unit tests and prod partial outages
        both flow through this path.
        """
        init_callable = getattr(self.bili_client, "get_room_init", None)
        if not callable(init_callable):
            logger.debug(
                "room_session: bili_client has no get_room_init, "
                "using input room_id={}",
                room_id,
            )
            return room_id
        # Cast: basedpyright sees ``object`` from getattr; runtime is an
        # AsyncMock in tests and the real coroutine in production.
        init_coro = cast(
            "Callable[[int], Awaitable[dict[str, object] | None]]",
            init_callable,
        )
        try:
            data = await init_coro(room_id)
        except Exception as exc:
            logger.warning(
                "room_session: get_room_init raised, using input room_id={} "
                "err={!r}",
                room_id,
                exc,
            )
            return room_id
        if not isinstance(data, dict):
            logger.warning(
                "room_session: get_room_init non-dict, using input room_id={}",
                room_id,
            )
            return room_id
        resolved = data.get("room_id")
        if isinstance(resolved, int) and resolved:
            if resolved != room_id:
                logger.info(
                    "room_session: resolved short_id={} -> real_id={}",
                    room_id,
                    resolved,
                )
            return resolved
        logger.warning(
            "room_session: get_room_init missing room_id, using input={}",
            room_id,
        )
        return room_id

    async def disconnect(self) -> None:
        """Stop the current client and reset state. No-op if not connected."""
        if self._client is None:
            return

        await self._client.stop()
        self._client = None
        self.status = "disconnected"
        # Reset dedup buffer — reconnecting is a fresh observation window.
        self._seen_ids.clear()
        self.room_id = None
        await self._broadcast(
            RoomStatusEvent(type="room_status", status="disconnected")
        )

    # ------------------------------------------------------------------
    # Callback registration
    # ------------------------------------------------------------------

    async def add_callback(self, cb: BridgeCallback) -> None:
        """Register ``cb`` to receive every future :class:`BridgeEvent`."""
        async with self._callback_lock:
            self._callbacks.append(cb)

    async def remove_callback(self, cb: BridgeCallback) -> None:
        """Unregister a previously-added callback. No-op if not present."""
        async with self._callback_lock:
            with contextlib.suppress(ValueError):
                self._callbacks.remove(cb)

    # ------------------------------------------------------------------
    # Raw B站 message ingestion (DanmakuClient.on_message)
    # ------------------------------------------------------------------

    async def _on_raw_message(self, raw: dict[str, object]) -> None:
        """Normalize raw → typed event, dedup, broadcast."""
        event = self._normalize(raw)
        if event is None:
            # Unsupported / malformed → silent skip (we never forward raw).
            return

        if isinstance(event, DanmakuEvent):
            # Dedup only DANMU_MSG by dm_v2 (ccShield convention).
            msg_id_obj = raw.get("dm_v2")
            msg_id = msg_id_obj if isinstance(msg_id_obj, str) else ""
            if msg_id:
                if msg_id in self._seen_ids:
                    return
                self._seen_ids.append(msg_id)
            # No msg_id → can't dedup; pass through.

        await self._broadcast(event)

    def _normalize(self, raw: dict[str, object]) -> BridgeEvent | None:
        """Turn a raw parsed B站 message into a typed :class:`BridgeEvent`.

        Returns ``None`` for unsupported cmds or malformed payloads.
        Never raises — defensive parsing throughout so a bad live frame
        cannot kill the broadcast loop.

        Field extraction sources (ported, not copied verbatim):

        * ``DANMU_MSG`` — ccShield ``app/core/danmaku_ws.py:401-431``
          (``info[1]=text``, ``info[2][0]=uid``, ``info[2][1]=uname``,
          ``info[7]=guard_level``, ``info[3]=medal``,
          ``info[0][4]=ts``).
        * ``SUPER_CHAT_MESSAGE`` / ``SUPER_CHAT_MESSAGE_JPN`` —
          ccShield ``app/core/danmaku_ws.py:451-472``
          (``data.uid``, ``data.user_info.uname``, ``data.message``,
          ``data.price``, ``data.start_time``).
        """
        if not isinstance(raw, dict):
            return None

        cmd_raw = raw.get("cmd")
        if not isinstance(cmd_raw, str) or not cmd_raw:
            return None
        # Strip variant suffix, e.g. "DANMU_MSG:4:0:2:2:2:0" → "DANMU_MSG".
        cmd = cmd_raw.partition(":")[0]

        if cmd == "DANMU_MSG":
            return self._normalize_danmu(raw)
        if cmd in ("SUPER_CHAT_MESSAGE", "SUPER_CHAT_MESSAGE_JPN"):
            return self._normalize_super_chat(raw)
        return None

    def _normalize_danmu(
        self, raw: dict[str, object]
    ) -> DanmakuEvent | None:
        """Extract a :class:`DanmakuEvent` from a DANMU_MSG payload.

        ``info[3]`` (medal) is ``[level, name, anchor_uname, ...]`` — level
        first, name second (per the live B站 API; older docs had them
        reversed).
        """
        info_obj = raw.get("info")
        if not isinstance(info_obj, list) or len(info_obj) < 3:
            return None

        info = info_obj

        # text — info[1]
        text_obj = info[1]
        text = text_obj if isinstance(text_obj, str) else ""

        # user: info[2][0]=uid, info[2][1]=uname
        user_obj = info[2]
        if not isinstance(user_obj, list) or len(user_obj) < 2:
            return None
        uid_obj = user_obj[0]
        uname_obj = user_obj[1]
        uid = uid_obj if isinstance(uid_obj, int) else 0
        uname = uname_obj if isinstance(uname_obj, str) else ""

        # ts — info[0][4] (server timestamp)
        ts = 0
        head_obj = info[0]
        if isinstance(head_obj, list) and len(head_obj) > 4:
            ts_obj = head_obj[4]
            if isinstance(ts_obj, int):
                ts = ts_obj

        # guard_level — info[7] if present
        guard_level = 0
        if len(info) > 7:
            gl_obj = info[7]
            if isinstance(gl_obj, int):
                guard_level = gl_obj

        # medal — info[3] = [level, name, anchor_uname, ...] (B站 live API order).
        medal: Medal | None = None
        if len(info) > 3 and info[3]:
            medal_raw = info[3]
            if isinstance(medal_raw, list) and len(medal_raw) >= 2:
                level_obj = medal_raw[0]
                name_obj = medal_raw[1]
                medal_level = level_obj if isinstance(level_obj, int) else 0
                medal_name = name_obj if isinstance(name_obj, str) else ""
                medal = Medal(name=medal_name, level=medal_level)

        return DanmakuEvent(
            type="danmaku",
            uid=uid,
            uname=uname,
            text=text,
            ts=ts,
            guard_level=guard_level,
            medal=medal,
        )

    def _normalize_super_chat(
        self, raw: dict[str, object]
    ) -> SuperChatEvent | None:
        """Extract a :class:`SuperChatEvent` from a SUPER_CHAT_* payload."""
        data_obj = raw.get("data")
        if not isinstance(data_obj, dict):
            return None
        data = data_obj

        uid_obj = data.get("uid")
        uid = uid_obj if isinstance(uid_obj, int) else 0

        uname = ""
        user_info_obj = data.get("user_info")
        if isinstance(user_info_obj, dict):
            uname_obj = user_info_obj.get("uname")
            if isinstance(uname_obj, str):
                uname = uname_obj

        msg_obj = data.get("message")
        text = msg_obj if isinstance(msg_obj, str) else ""

        price_obj = data.get("price")
        price = price_obj if isinstance(price_obj, int) else 0

        ts_obj = data.get("start_time")
        ts = ts_obj if isinstance(ts_obj, int) else 0

        return SuperChatEvent(
            type="sc",
            uid=uid,
            uname=uname,
            text=text,
            price=price,
            ts=ts,
        )

    # ------------------------------------------------------------------
    # Internal: fan-out
    # ------------------------------------------------------------------

    async def _broadcast(self, event: BridgeEvent) -> None:
        """Snapshot callbacks under lock, then await each (errors logged)."""
        async with self._callback_lock:
            snapshot = list(self._callbacks)

        if not snapshot:
            logger.debug(
                "room_session: broadcast with 0 callbacks room={} type={}",
                self.room_id,
                event.type,
            )
            return

        for cb in snapshot:
            try:
                await cb(event)
            except Exception as exc:
                logger.error(
                    "room_session: callback raised room={} err={!r}",
                    self.room_id,
                    exc,
                )


__all__ = [
    "DEFAULT_DEDUP_SIZE",
    "RoomSession",
]
