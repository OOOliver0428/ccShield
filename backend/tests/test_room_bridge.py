"""High-load fan-out tests for per-browser WebSocket isolation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import cast
from unittest.mock import AsyncMock

from starlette.websockets import WebSocket

from app.room.bridge import RoomBridge
from app.room.events import DanmakuEvent
from app.room.session import RoomSession


class _SlowWebSocket:
    def __init__(self) -> None:
        self.send_started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def send_json(self, _payload: dict[str, object]) -> None:
        self.send_started.set()
        await self.release.wait()

    async def close(self) -> None:
        self.closed = True
        self.release.set()


class _FastWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.closed = False

    async def send_json(self, payload: dict[str, object]) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True


def _mock_session() -> AsyncMock:
    session = AsyncMock(spec=RoomSession)
    session.room_id = 22210347
    session.initial_super_chats = []
    return session


def _event(index: int) -> DanmakuEvent:
    return DanmakuEvent(
        type="danmaku",
        uid=index,
        uname=f"user-{index}",
        text=f"message-{index}",
        ts=int(time.time()),
        guard_level=0,
        medal=None,
    )


async def _wait_until(
    predicate: Callable[[], bool],
    *,
    poll_interval: float = 0,
) -> None:
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(poll_interval)
    raise AssertionError("condition was not reached")


async def test_slow_browser_never_blocks_fast_browser_or_upstream() -> None:
    session = _mock_session()
    bridge = RoomBridge(
        session,
        _client_queue_maxsize=1,
        _send_timeout=5.0,
    )
    slow = _SlowWebSocket()
    fast = _FastWebSocket()
    slow_ws = cast(WebSocket, slow)
    fast_ws = cast(WebSocket, fast)
    await bridge.register_ws(slow_ws)
    await bridge.register_ws(fast_ws)

    await bridge._on_event(_event(1))
    await _wait_until(lambda: slow.send_started.is_set() and len(fast.sent) == 1)

    # The slow sender is stuck on message 1. Message 2 fills its private
    # queue while the fast sender remains fully caught up.
    await bridge._on_event(_event(2))
    await _wait_until(lambda: len(fast.sent) == 2)

    # Message 3 overflows only the slow peer. Upstream enqueue remains
    # non-blocking and the fast peer still receives the event.
    await asyncio.wait_for(bridge._on_event(_event(3)), timeout=0.1)
    await _wait_until(lambda: len(fast.sent) == 3 and slow.closed)

    assert bridge.client_count == 1
    assert [payload["text"] for payload in fast.sent] == [
        "message-1",
        "message-2",
        "message-3",
    ]
    assert bridge.metrics_snapshot["slow_clients_dropped"] == 1

    await bridge.close()
    session.remove_callback.assert_awaited_once()


async def test_sender_timeout_disconnects_stalled_browser() -> None:
    session = _mock_session()
    bridge = RoomBridge(
        session,
        _client_queue_maxsize=10,
        _send_timeout=0.001,
    )
    slow = _SlowWebSocket()
    await bridge.register_ws(cast(WebSocket, slow))

    await bridge._on_event(_event(1))
    await _wait_until(
        lambda: bridge.client_count == 0 and slow.closed,
        poll_interval=0.001,
    )

    assert bridge.metrics_snapshot["send_failures"] == 1
    await bridge.close()
