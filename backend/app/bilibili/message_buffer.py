"""Priority-aware in-memory buffer for live-room messages.

The B站 socket carries chat, paid messages, room-management events, gifts,
rankings, entry effects and other telemetry on the same stream. Under burst
load a plain bounded FIFO lets low-value traffic occupy every slot and then
drops the next message indiscriminately. This buffer keeps three FIFO lanes
and, when full, lets a higher-priority message evict the oldest lower-priority
entry before it considers dropping the incoming item.

The buffer deliberately remains bounded. If every slot already contains
critical messages, the newest critical message is rejected rather than
growing memory without limit. That condition is observable through the
client metrics and an error-level log in ``danmaku_ws.py``.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from enum import IntEnum
from typing import Final


class MessagePriority(IntEnum):
    CRITICAL = 0
    NORMAL = 1
    LOW = 2


_CRITICAL_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        "DANMU_MSG",
        "SUPER_CHAT_MESSAGE",
        "SUPER_CHAT_MESSAGE_JPN",
        "SUPER_CHAT_MESSAGE_DELETE",
        # Preserve room-management/control events for the next moderation
        # phase even before RoomSession normalizes all of them.
        "ROOM_BLOCK_MSG",
        "WARNING",
        "CUT_OFF",
    }
)

_NORMAL_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        "GUARD_BUY",
        "SEND_GIFT",
        "ROOM_ADMIN_ENTRANCE",
        "ROOM_ADMINS",
        "LIVE",
        "PREPARING",
    }
)


def command_name(message: dict[str, object]) -> str:
    value = message.get("cmd")
    return value.partition(":")[0] if isinstance(value, str) else "<unknown>"


def classify_message(message: dict[str, object]) -> MessagePriority:
    command = command_name(message)
    if command in _CRITICAL_COMMANDS:
        return MessagePriority.CRITICAL
    if command in _NORMAL_COMMANDS:
        return MessagePriority.NORMAL
    return MessagePriority.LOW


@dataclass(frozen=True, slots=True)
class BufferedMessage:
    payload: dict[str, object]
    priority: MessagePriority
    enqueued_at: float


@dataclass(frozen=True, slots=True)
class BufferPutResult:
    accepted: bool
    dropped: BufferedMessage | None = None


class PriorityMessageBuffer:
    """Single-consumer, bounded priority buffer for one asyncio event loop."""

    def __init__(self, maxsize: int) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        self.maxsize = maxsize
        self._queues: tuple[deque[BufferedMessage], ...] = tuple(
            deque() for _ in MessagePriority
        )
        self._size = 0
        self._not_empty = asyncio.Event()

    def qsize(self) -> int:
        return self._size

    def empty(self) -> bool:
        return self._size == 0

    def put_nowait(
        self,
        payload: dict[str, object],
        *,
        enqueued_at: float,
    ) -> BufferPutResult:
        priority = classify_message(payload)
        incoming = BufferedMessage(payload, priority, enqueued_at)
        dropped: BufferedMessage | None = None

        if self._size >= self.maxsize:
            # Search from the lowest lane up, but only evict a message that
            # is strictly less important than the incoming one.
            for victim_priority in range(
                int(MessagePriority.LOW), int(priority), -1
            ):
                victim_queue = self._queues[victim_priority]
                if victim_queue:
                    dropped = victim_queue.popleft()
                    self._size -= 1
                    break

            if dropped is None:
                return BufferPutResult(accepted=False, dropped=incoming)

        self._queues[int(priority)].append(incoming)
        self._size += 1
        self._not_empty.set()
        return BufferPutResult(accepted=True, dropped=dropped)

    async def get(self) -> BufferedMessage:
        while self._size == 0:
            self._not_empty.clear()
            # Recheck after clear: producer and consumer run on one event
            # loop, but this keeps the condition correct if the class is
            # ever called from a scheduled callback between operations.
            if self._size == 0:
                await self._not_empty.wait()

        for queue in self._queues:
            if queue:
                item = queue.popleft()
                self._size -= 1
                if self._size == 0:
                    self._not_empty.clear()
                return item
        raise RuntimeError("buffer size invariant violated")

    def clear(self) -> None:
        for queue in self._queues:
            queue.clear()
        self._size = 0
        self._not_empty.clear()


__all__ = [
    "BufferPutResult",
    "BufferedMessage",
    "MessagePriority",
    "PriorityMessageBuffer",
    "classify_message",
    "command_name",
]
