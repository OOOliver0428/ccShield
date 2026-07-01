"""TDD tests for RoomSession (T12) + typed BridgeEvent schema (T12).

These tests predate ``app/room/`` and are intended to be RUN FIRST — the
import errors at collection time, and once the module is implemented
each test exercises a single invariant. No real network: the T11
``DanmakuClient`` is patched at the session module level with a
``FakeDanmakuClient`` that records ``start``/``stop`` calls.

Test count: 10 (one per spec scenario):

  1. ``_normalize`` DANMU_MSG basic
  2. ``_normalize`` DANMU_MSG with cmd suffix ``"DANMU_MSG:4:0:2:2:2:0"``
  3. ``_normalize`` DANMU_MSG no medal
  4. ``_normalize`` SUPER_CHAT_MESSAGE
  5. ``_normalize`` unknown cmd / missing cmd
  6. ``_normalize`` malformed DANMU_MSG
  7. Dedup: same ``dm_v2`` twice → broadcast once
  8. ``connect`` (start=True) + ``disconnect`` broadcasts status
  9. ``connect`` room A then room B → A.stop() called first
 10. Broadcast error isolation: one raising callback does not break others
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast
from unittest.mock import AsyncMock

import pytest

from app.room.events import (
    BridgeEvent,
    DanmakuEvent,
    Medal,
    RoomStatusEvent,
    SuperChatEvent,
)
from app.room.session import RoomSession

if TYPE_CHECKING:
    from app.bilibili.client import BilibiliClient


def _stub_bili() -> BilibiliClient:
    """Cheap stand-in for tests that never invoke the bili client.

    ``RoomSession.__init__`` stores it; only ``connect()`` actually
    uses it, and every connect-related test patches
    ``app.room.session.DanmakuClient`` so the real client is never
    touched. ``cast`` here is a runtime no-op that lets basedpyright
    see the right type without importing the heavy module.
    """
    return cast("BilibiliClient", object())


class _StubBiliWithInit:
    """Stand-in that DOES have a settable ``get_room_init`` for resolver tests."""

    def __init__(self) -> None:
        self.get_room_init: AsyncMock | None = None

# ---------------------------------------------------------------------------
# FakeDanmakuClient — drop-in for app.bilibili.danmaku_ws.DanmakuClient.
# Never opens a socket. start() returns whatever start_return is set to;
# stop() is a no-op that records itself. class-level `instances` lets
# tests check start/stop ordering across reconnects.
# ---------------------------------------------------------------------------


class FakeDanmakuClient:
    """Drop-in replacement for ``app.bilibili.danmaku_ws.DanmakuClient``."""

    instances: ClassVar[list[FakeDanmakuClient]] = []

    # Class-level so a test can flip it via
    # ``FakeDanmakuClient.start_return = False`` BEFORE patches.
    start_return: ClassVar[bool] = True

    def __init__(
        self,
        room_id: int,
        bili_client: Any,
        on_message: Any | None = None,
    ) -> None:
        self.room_id = room_id
        self.bili_client = bili_client
        self.on_message = on_message
        self.started: bool = False
        self.stopped: bool = False
        type(self).instances.append(self)

    async def start(self) -> bool:
        self.started = True
        # Read from class so a pre-test ``FakeDanmakuClient.start_return = X``
        # is honoured by every instance constructed inside the patch.
        return type(self).start_return

    async def stop(self) -> None:
        self.stopped = True

    @classmethod
    def reset(cls) -> None:
        cls.instances = []


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    FakeDanmakuClient.reset()
    # ``start_return`` is a class-level hook flipped by some tests;
    # restoring True between tests prevents cross-test pollution.
    FakeDanmakuClient.start_return = True


# ---------------------------------------------------------------------------
# 1. _normalize DANMU_MSG basic — info[1]=text, info[2][0]=uid,
#    info[2][1]=uname, info[0][4]=ts, info[3]=[name,level], info[7]=guard
# ---------------------------------------------------------------------------


def test_normalize_danmu_msg_basic() -> None:
    """info[3] = [level, name, ...] (B站 live API order) → Medal(name=name, level=level)."""
    sess = RoomSession(bili_client=_stub_bili())
    raw = {
        "cmd": "DANMU_MSG",
        "info": [
            [0, 0, 0, 0, 1700000000, 0],  # info[0] — ts at index 4
            "hello",                       # info[1] — text
            [123, "alice", 0, 0, 0],       # info[2] — uid=123, uname="alice"
            [5, "粉丝团", "anchor1"],         # info[3] — [level, name, anchor]
        ],
    }
    event = sess._normalize(raw)
    assert isinstance(event, DanmakuEvent)
    assert event.type == "danmaku"
    assert event.uid == 123
    assert event.uname == "alice"
    assert event.text == "hello"
    assert event.ts == 1700000000
    assert event.guard_level == 0  # info has <8 elements → default 0
    assert isinstance(event.medal, Medal)
    assert event.medal.name == "粉丝团"
    assert event.medal.level == 5


# ---------------------------------------------------------------------------
# 2. _normalize DANMU_MSG with cmd suffix "DANMU_MSG:4:0:2:2:2:0"
# ---------------------------------------------------------------------------


def test_normalize_danmu_msg_with_cmd_suffix() -> None:
    """cmd=='DANMU_MSG:4:0:2:2:2:0' must partition on ':' and still normalize."""
    sess = RoomSession(bili_client=_stub_bili())
    raw = {
        "cmd": "DANMU_MSG:4:0:2:2:2:0",
        "info": [
            [0, 0, 0, 0, 1700000000, 0],
            "world",
            [777, "bob", 0, 0, 0],
        ],
    }
    event = sess._normalize(raw)
    assert isinstance(event, DanmakuEvent)
    assert event.text == "world"
    assert event.uid == 777
    assert event.uname == "bob"
    assert event.medal is None


# ---------------------------------------------------------------------------
# 3. _normalize DANMU_MSG with no medal (info[3] absent or falsy)
# ---------------------------------------------------------------------------


def test_normalize_danmu_msg_no_medal() -> None:
    """info[3] missing OR None → medal is None (must not crash)."""
    sess = RoomSession(bili_client=_stub_bili())

    # Case A: info[3] absent (info has 3 elements).
    raw_a = {
        "cmd": "DANMU_MSG",
        "info": [
            [0, 0, 0, 0, 1700000000, 0],
            "no-medal",
            [1, "u", 0, 0, 0],
        ],
    }
    event_a = sess._normalize(raw_a)
    assert isinstance(event_a, DanmakuEvent)
    assert event_a.medal is None

    # Case B: info[3] explicitly None.
    raw_b = {
        "cmd": "DANMU_MSG",
        "info": [
            [0, 0, 0, 0, 1700000000, 0],
            "no-medal-2",
            [1, "u", 0, 0, 0],
            None,
        ],
    }
    event_b = sess._normalize(raw_b)
    assert isinstance(event_b, DanmakuEvent)
    assert event_b.medal is None


# ---------------------------------------------------------------------------
# 4. _normalize SUPER_CHAT_MESSAGE
# ---------------------------------------------------------------------------


def test_normalize_super_chat() -> None:
    """data.uid, data.user_info.uname, data.message, data.price,
    data.start_time → SuperChatEvent."""
    sess = RoomSession(bili_client=_stub_bili())
    raw = {
        "cmd": "SUPER_CHAT_MESSAGE",
        "data": {
            "uid": 456,
            "user_info": {"uname": "bob"},
            "message": "hi",
            "price": 30,
            "start_time": 1700000001,
        },
    }
    event = sess._normalize(raw)
    assert isinstance(event, SuperChatEvent)
    assert event.type == "sc"
    assert event.uid == 456
    assert event.uname == "bob"
    assert event.text == "hi"
    assert event.price == 30
    assert event.ts == 1700000001


# ---------------------------------------------------------------------------
# 5. _normalize unknown / missing cmd → None
# ---------------------------------------------------------------------------


def test_normalize_unknown_cmd_returns_none() -> None:
    """Unsupported cmds return None (never crash, never raise)."""
    sess = RoomSession(bili_client=_stub_bili())

    assert sess._normalize({"cmd": "SEND_GIFT", "data": {}}) is None
    assert sess._normalize({"cmd": "INTERACT_WORD", "data": {}}) is None
    assert sess._normalize({}) is None  # missing cmd
    assert sess._normalize({"cmd": ""}) is None  # empty cmd


# ---------------------------------------------------------------------------
# 6. _normalize malformed DANMU_MSG — info too short, non-dict raw, etc.
# ---------------------------------------------------------------------------


def test_normalize_malformed_danmu_msg_returns_none() -> None:
    """Defensive parsing: short info / non-dict raw → None, no exceptions."""
    sess = RoomSession(bili_client=_stub_bili())

    # info missing entirely.
    assert sess._normalize({"cmd": "DANMU_MSG"}) is None

    # info too short.
    assert sess._normalize({"cmd": "DANMU_MSG", "info": [1]}) is None
    assert sess._normalize(
        {
            "cmd": "DANMU_MSG",
            "info": [[0, 0, 0, 0, 0, 0], "x"],  # only 2 elements
        }
    ) is None

    # info[2] missing uid/uname.
    assert sess._normalize(
        {
            "cmd": "DANMU_MSG",
            "info": [[0, 0, 0, 0, 0, 0], "x", []],  # info[2] empty
        }
    ) is None

    # Non-dict raw must not crash.
    assert sess._normalize(None) is None  # type: ignore[arg-type]
    assert sess._normalize("not-a-dict") is None  # type: ignore[arg-type]
    assert sess._normalize(42) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 7. Dedup: same dm_v2 twice → broadcast once.
# ---------------------------------------------------------------------------


async def test_dedup_same_dm_v2_twice() -> None:
    """Identical DANMU_MSG with same dm_v2 → second is dropped, broadcast once."""
    sess = RoomSession(bili_client=_stub_bili())
    received: list[BridgeEvent] = []

    async def cb(event: BridgeEvent) -> None:
        received.append(event)

    await sess.add_callback(cb)

    raw = {
        "cmd": "DANMU_MSG",
        "dm_v2": "msg-1",
        "info": [
            [0, 0, 0, 0, 1700000000, 0],
            "x",
            [1, "u", 0, 0, 0],
        ],
    }
    await sess._on_raw_message(raw)
    await sess._on_raw_message(raw)  # duplicate
    assert len(received) == 1


# ---------------------------------------------------------------------------
# 8. connect(start=True) + disconnect → RoomStatusEvent broadcasts.
# ---------------------------------------------------------------------------


async def test_connect_disconnect_updates_status_and_broadcasts() -> None:
    """start()=True → status='connected' + RoomStatusEvent('connected').
    disconnect() → status='disconnected' + RoomStatusEvent('disconnected').
    """
    sess = RoomSession(bili_client=_stub_bili())
    events: list[BridgeEvent] = []

    async def cb(event: BridgeEvent) -> None:
        events.append(event)

    await sess.add_callback(cb)

    with patch_real_dc():
        ok = await sess.connect(100)

    assert ok is True
    assert sess.status == "connected"
    assert sess.room_id == 100
    assert len(FakeDanmakuClient.instances) == 1
    inst_a = FakeDanmakuClient.instances[0]
    assert inst_a.started is True
    assert inst_a.stopped is False

    status_events = [e for e in events if isinstance(e, RoomStatusEvent)]
    assert any(e.status == "connected" for e in status_events)

    await sess.disconnect()
    assert sess.status == "disconnected"
    assert sess.room_id is None
    assert inst_a.stopped is True
    # Re-evaluate: disconnect appends a new RoomStatusEvent.
    status_events = [e for e in events if isinstance(e, RoomStatusEvent)]
    assert any(e.status == "disconnected" for e in status_events)


# ---------------------------------------------------------------------------
# (bonus) connect start=False → status='error', broadcast RoomStatusEvent(error)
# ---------------------------------------------------------------------------


async def test_connect_start_failure_emits_error_status() -> None:
    """start()=False → status='error' + RoomStatusEvent('error'); False returned."""
    sess = RoomSession(bili_client=_stub_bili())
    FakeDanmakuClient.start_return = False

    events: list[BridgeEvent] = []

    async def cb(event: BridgeEvent) -> None:
        events.append(event)

    await sess.add_callback(cb)

    with patch_real_dc():
        ok = await sess.connect(100)

    assert ok is False
    assert sess.status == "error"
    status_events = [e for e in events if isinstance(e, RoomStatusEvent)]
    assert any(e.status == "error" for e in status_events)


# ---------------------------------------------------------------------------
# 9. Single active room: connect B → A.stop() called first.
# ---------------------------------------------------------------------------


async def test_connect_room_b_disconnects_room_a_first() -> None:
    """Reconnecting to a new room must stop the previous client cleanly."""
    sess = RoomSession(bili_client=_stub_bili())

    with patch_real_dc():
        ok_a = await sess.connect(100)
        client_a = FakeDanmakuClient.instances[0]
        ok_b = await sess.connect(200)
        client_b = FakeDanmakuClient.instances[1]

    assert ok_a is True
    assert ok_b is True
    assert client_a.stopped is True, "A.stop() must be called before B is started"
    assert client_b.stopped is False
    assert client_a.started is True
    assert client_b.started is True
    assert sess.room_id == 200


# ---------------------------------------------------------------------------
# 11. RoomSession.connect must resolve a SHORT room id to the REAL id
#     via bili_client.get_room_init before constructing the DanmakuClient.
#     ccShield's proven RoomManager does this via resolve_room_id; without
#     it, B站 rejects the AUTH frame and "no danmaku" loads.
# ---------------------------------------------------------------------------


async def test_connect_resolves_short_id_to_real_via_bili_init() -> None:
    """Given a short room id, ``connect`` must look up the real id via
    ``bili_client.get_room_init`` and pass the resolved id — not the
    input — to the underlying DanmakuClient.
    """
    bili = cast("BilibiliClient", _StubBiliWithInit())
    bili.get_room_init = AsyncMock(
        return_value={"room_id": 99999, "short_id": 42, "uid": 7}
    )

    sess = RoomSession(bili_client=bili)

    with patch_real_dc():
        ok = await sess.connect(42)  # input is the SHORT id

    assert ok is True
    bili.get_room_init.assert_awaited_once_with(42)
    assert len(FakeDanmakuClient.instances) == 1
    inst = FakeDanmakuClient.instances[0]
    # The DanmakuClient must have been constructed with the REAL id.
    assert inst.room_id == 99999, (
        f"DanmakuClient must receive resolved real room id 99999, "
        f"got {inst.room_id}"
    )
    # And sess.room_id surfaces the resolved id, not the input.
    assert sess.room_id == 99999


async def test_connect_falls_back_to_input_room_id_when_resolve_fails() -> None:
    """If ``get_room_init`` is unavailable (stub bili, network error), the
    existing input id is used as-is — preserves backward-compatibility
    with the unit tests that don't stub the bili client.
    """
    sess = RoomSession(bili_client=_stub_bili())  # no get_room_init

    with patch_real_dc():
        ok = await sess.connect(22210347)

    assert ok is True
    inst = FakeDanmakuClient.instances[0]
    assert inst.room_id == 22210347  # untouched — no resolver call possible
    assert sess.room_id == 22210347


# ---------------------------------------------------------------------------
# 10. Broadcast error isolation: one raising callback must NOT prevent others.
# ---------------------------------------------------------------------------


async def test_broadcast_error_is_isolated() -> None:
    """A raising callback does not abort the broadcast and does not propagate."""
    sess = RoomSession(bili_client=_stub_bili())
    good_received: list[BridgeEvent] = []
    good2_received: list[BridgeEvent] = []

    async def good(event: BridgeEvent) -> None:
        good_received.append(event)

    async def bad(event: BridgeEvent) -> None:
        raise RuntimeError("boom")

    async def good2(event: BridgeEvent) -> None:
        good2_received.append(event)

    await sess.add_callback(good)
    await sess.add_callback(bad)
    await sess.add_callback(good2)

    raw = {
        "cmd": "DANMU_MSG",
        "dm_v2": "msg-iso",
        "info": [
            [0, 0, 0, 0, 1700000000, 0],
            "x",
            [1, "u", 0, 0, 0],
        ],
    }

    # Must not raise even though `bad` raises.
    await sess._on_raw_message(raw)

    assert len(good_received) == 1
    assert len(good2_received) == 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def patch_real_dc() -> Any:
    """Return a context manager that patches app.room.session.DanmakuClient
    with our ``FakeDanmakuClient`` for the duration of the ``with``.
    """
    from unittest.mock import patch

    return patch("app.room.session.DanmakuClient", new=FakeDanmakuClient)
