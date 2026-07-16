"""Room session package.

Owns the single-room session lifecycle (``RoomSession``) and the typed
``BridgeEvent`` schema that flows out of it. This package is the only
bridge between the B站 wire protocol and the FastAPI/WebSocket
layer — neither side sees the raw ``cmd``/``info`` dicts.
"""

from app.room.events import (
    BridgeEvent,
    DanmakuEvent,
    Medal,
    RoomStatusEvent,
    SuperChatEvent,
)
from app.room.session import DEFAULT_DEDUP_SIZE, RoomSession

__all__ = [
    "DEFAULT_DEDUP_SIZE",
    "BridgeEvent",
    "DanmakuEvent",
    "Medal",
    "RoomSession",
    "RoomStatusEvent",
    "SuperChatEvent",
]
