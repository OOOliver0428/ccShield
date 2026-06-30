"""Typed BridgeEvent schema (T12).

The Bili live-WS protocol hands us raw cmd/info dicts (see
:mod:`app.bilibili.protocol`). For the rest of the app (FastAPI
routes, WebSocket bridge, future persistence) we do NOT forward that
raw shape — every emitted event is one of the typed models below.

Why type them up?

* Frontend never has to know about B站 cmd/info. A websocket client can
  switch on ``BridgeEvent.type`` and trust the field names.
* Renaming a B站 field in T11 stays contained: a single normalize step
  lives in :mod:`app.room.session`.
* Pydantic v2 gives us free validation + JSON serialization for the
  WS payload.

``BridgeEvent`` is a closed PEP 604 union — adding a new variant is a
deliberate, reviewable change.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class Medal(BaseModel):
    """粉丝牌 (channel/medal) info attached to a danmaku.

    Field convention matches the test fixtures: B站 sends the medal
    as ``[medal_name, medal_level]`` (name first, level second). See
    test_normalize_danmu_msg_basic in ``tests/test_room_session.py``.
    """

    name: str
    level: int


class DanmakuEvent(BaseModel):
    """A single chat message.

    Field extraction (ported from
    ``ccShield/app/core/danmaku_ws.py:401-431``):

    * ``text``   — ``info[1]`` (raw message text)
    * ``uid``    — ``info[2][0]`` (sender uid)
    * ``uname``  — ``info[2][1]`` (sender username)
    * ``ts``     — ``info[0][4]`` (server-reported timestamp, seconds)
    * ``guard_level`` — ``info[7]`` if present, else 0
      (0=none, 1=舰长, 2=提督, 3=总督)
    * ``medal``  — ``info[3]`` if present and non-empty (parsed as
      :class:`Medal`); None otherwise
    """

    type: Literal["danmaku"]
    uid: int
    uname: str
    text: str
    ts: int
    guard_level: int
    medal: Medal | None


class SuperChatEvent(BaseModel):
    """A Super Chat (醒目留言) message.

    Field extraction (ported from
    ``ccShield/app/core/danmaku_ws.py:451-472``):

    * ``uid``    — ``data.uid``
    * ``uname``  — ``data.user_info.uname``
    * ``text``   — ``data.message``
    * ``price``  — ``data.price`` (RMB cents? B站 reports as integer)
    * ``ts``     — ``data.start_time`` (server-reported timestamp)
    """

    type: Literal["sc"]
    uid: int
    uname: str
    text: str
    price: int
    ts: int


class RoomStatusEvent(BaseModel):
    """Connection-state events emitted by :class:`RoomSession` itself.

    These are NOT derived from B站 messages — they describe the local
    session lifecycle (connected / disconnected / reconnecting / error).
    """

    type: Literal["room_status"]
    status: Literal["connected", "disconnected", "reconnecting", "error"]


# Closed union of every event a callback may receive. Consumers can
# switch on ``event.type`` (``"danmaku" | "sc" | "room_status"``) and
# isinstance against the concrete model.
BridgeEvent = DanmakuEvent | SuperChatEvent | RoomStatusEvent


__all__ = [
    "BridgeEvent",
    "DanmakuEvent",
    "Medal",
    "RoomStatusEvent",
    "SuperChatEvent",
]
