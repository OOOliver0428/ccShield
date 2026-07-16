"""TDD tests for Bili live-WS DanmakuClient (T11).

Six tests, fully mocked — no real network, no real asyncio.sleep.

Mock strategy (where the previous attempt got stuck):

1. ``FakeWS`` — a tiny in-memory fake that yields the bytes you pre-load
   in order, then raises ``ConnectionClosed`` on the next ``recv()``.
   This makes both the listen-loop-exit and the reconnect-after-disconnect
   paths deterministic without elaborate ``asyncio.Event`` machinery.

2. ``websockets.connect`` is patched per-test so the production code calls
   our fake instead of opening a real socket. The patch targets
   ``app.bilibili.danmaku_ws.websockets.connect`` (the module-level
   reference) so we never need to mock the ``websockets`` module itself.

3. ``asyncio.sleep`` is patched with a helper that yields once to the
   real loop (``await real_sleep(0)``). A bare ``AsyncMock()`` would
   resolve immediately but NOT actually yield control, which would
   starve the connect / listen / heartbeat tasks. Real ``sleep(0)``
   gives the scheduler a chance to run them.

4. ``bili_client.get_danmu_info`` and ``get_user_info`` are replaced by
   a small ``AsyncMock`` wrapper that returns canned payloads.

5. Test seams (``_heartbeat_interval``, ``_watchdog_timeout``,
   ``_reconnect_delays``, ``_reconnect_max_attempts``, ``_queue_maxsize``,
   ``_auth_timeout``) keep every test under 2 s wall clock.
"""
from __future__ import annotations

import asyncio
import json
import struct
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from websockets.exceptions import ConnectionClosed

from app.bilibili import protocol as proto
from app.bilibili.exceptions import AuthExpiredError

# Real sleep, captured BEFORE any test patches asyncio.sleep. Used by the
# _yield_sleep helper to actually yield control without sleeping.
_real_sleep = asyncio.sleep


async def _yield_sleep(_delay: float) -> None:
    """Replace ``asyncio.sleep`` in tests: yield once, ignore the delay."""
    await _real_sleep(0)


# ---------------------------------------------------------------------------
# FakeWebSocket — pre-loaded frames, then ConnectionClosed on next recv.
# ---------------------------------------------------------------------------


class FakeWebSocket:
    """Fake that drains a pre-loaded list of bytes, then disconnects."""

    def __init__(self, recv_frames: list[bytes] | None = None) -> None:
        self._recv: list[bytes] = list(recv_frames or [])
        self.sent: list[bytes] = []
        self.closed: bool = False

    async def recv(self) -> bytes:
        if self.closed:
            raise ConnectionClosed(None, None)
        if not self._recv:
            raise ConnectionClosed(None, None)
        return self._recv.pop(0)

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def make_danmu_info_payload(
    *, token: str = "tok-abc", hosts: int = 1
) -> dict[str, Any]:
    """Build a getDanmuInfo-shaped `data` payload."""
    return {
        "group": "live",
        "business_id": 0,
        "refresh_row_factor": 0.125,
        "refresh_rate": 100,
        "max_delay": 5000,
        "token": token,
        "host_list": [
            {"host": f"chat{i}.bilibili.com", "port": 2245, "wss_port": 443}
            for i in range(hosts)
        ],
    }


def make_bili_client(
    *,
    danmu_info: dict[str, Any] | None = None,
    user_info: dict[str, Any] | None = None,
    buvid3: str | None = None,
) -> Any:
    """Build a mock BilibiliClient with canned get_danmu_info + get_user_info."""
    if danmu_info is None:
        danmu_info = make_danmu_info_payload()

    async def _get_danmu_info(_room_id: int) -> dict[str, Any]:
        return danmu_info

    async def _get_user_info() -> dict[str, Any] | None:
        return user_info

    cli = AsyncMock()
    cli.get_danmu_info = AsyncMock(side_effect=_get_danmu_info)
    cli.get_user_info = AsyncMock(side_effect=_get_user_info)
    cli.get_cookie = Mock(
        side_effect=lambda name: buvid3 if name == "buvid3" else None
    )
    return cli


def pack_auth_rsp(code: int) -> bytes:
    """Build a real AUTH_RSP frame using T3 pack_data."""
    body = json.dumps({"code": code}, separators=(",", ":")).encode("utf-8")
    return proto.pack_data(body, proto.AUTH_RSP)


def pack_danmu_msg(cmd: str = "DANMU_MSG", dm_v2: str = "v2-1") -> bytes:
    """Build a real NORMAL frame containing a DANMU_MSG-like dict."""
    body = json.dumps(
        {
            "cmd": cmd,
            "dm_v2": dm_v2,
            "info": [1.0, "hello", [101, "alice", 0, 0, 0, 0, 0, 0]],
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return proto.pack_data(body, proto.NORMAL)


def pack_heartbeat_rsp(online_count: int = 42) -> bytes:
    """Build a HEARTBEAT_RSP frame (4-byte big-endian online count)."""
    payload = struct.pack(">I", online_count)
    return proto.pack_data(payload, proto.HEARTBEAT_RSP)


def is_heartbeat_frame(raw: bytes) -> bool:
    """True iff ``raw`` is a HEARTBEAT (packet_type 2) frame."""
    if len(raw) < 16:
        return False
    _total, _h, _pv, pt, _seq = struct.unpack(">IHHII", raw[:16])
    return pt == proto.HEARTBEAT


def is_auth_frame(raw: bytes) -> bool:
    """True iff ``raw`` is an AUTH (packet_type 7) frame."""
    if len(raw) < 16:
        return False
    _total, _h, _pv, pt, _seq = struct.unpack(">IHHII", raw[:16])
    return pt == proto.AUTH


def auth_frame_proto_ver(raw: bytes) -> int:
    """Return the protover field from an AUTH frame header."""
    if len(raw) < 16:
        return -1
    _total, _h, pv, pt, _seq = struct.unpack(">IHHII", raw[:16])
    assert pt == proto.AUTH, f"frame is not AUTH (got pt={pt})"
    return pv


async def test_start_propagates_cookie_expiry_to_room_boundary() -> None:
    from app.bilibili.danmaku_ws import DanmakuClient

    bili = make_bili_client()
    bili.get_danmu_info = AsyncMock(side_effect=AuthExpiredError("expired"))
    client = DanmakuClient(1601605, bili)

    with pytest.raises(AuthExpiredError):
        await client.start()


# ---------------------------------------------------------------------------
# Test 1 — start() success path.
# ---------------------------------------------------------------------------
async def test_start_success_forwards_danmaku_to_on_message() -> None:
    """Mock 1 host, fake ws yields AUTH_RSP(code 0) then a NORMAL msg.

    Expect start() to return True within 2 s and on_message to be invoked
    with the parsed danmaku dict.
    """
    bili = make_bili_client(user_info={"mid": 123, "uname": "alice"})
    fakes: list[FakeWebSocket] = []

    async def fake_connect(_url: str, **_kw: Any) -> FakeWebSocket:
        ws = FakeWebSocket(
            recv_frames=[pack_auth_rsp(0), pack_danmu_msg(dm_v2="hello-1")]
        )
        fakes.append(ws)
        return ws

    received: list[dict[str, Any]] = []

    async def on_msg(msg: dict[str, Any]) -> None:
        received.append(msg)

    with (
        patch("app.bilibili.danmaku_ws.websockets.connect", new=fake_connect),
        patch("app.bilibili.danmaku_ws.asyncio.sleep", new=_yield_sleep),
    ):
        from app.bilibili.danmaku_ws import DanmakuClient

        client = DanmakuClient(
            room_id=100,
            bili_client=bili,
            on_message=on_msg,
            _auth_timeout=2.0,
            _heartbeat_interval=999.0,
            _watchdog_timeout=999.0,
            _reconnect_delays=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        )

        ok = await asyncio.wait_for(client.start(), timeout=2.0)

        # Yield until the consumer drains the danmu msg (or timeout).
        for _ in range(50):
            if any(m.get("cmd") == "DANMU_MSG" for m in received):
                break
            await _real_sleep(0)

    assert ok is True
    danmu_msgs = [m for m in received if m.get("cmd") == "DANMU_MSG"]
    assert len(danmu_msgs) >= 1, received
    assert danmu_msgs[0].get("dm_v2") == "hello-1"

    await asyncio.wait_for(client.stop(), timeout=6.0)


# ---------------------------------------------------------------------------
# Test 1b — AUTH packet is raw even when its JSON requests Brotli messages.
# ---------------------------------------------------------------------------


async def test_auth_frame_header_is_raw_protover_1() -> None:
    """AUTH header version is 1; JSON ``protover`` independently stays 3."""
    bili = make_bili_client(user_info={"mid": 1})
    fakes: list[FakeWebSocket] = []

    async def fake_connect(_url: str, **_kw: Any) -> FakeWebSocket:
        ws = FakeWebSocket(recv_frames=[pack_auth_rsp(0)])
        fakes.append(ws)
        return ws

    received: list[dict[str, Any]] = []

    async def on_msg(msg: dict[str, Any]) -> None:
        received.append(msg)

    with (
        patch("app.bilibili.danmaku_ws.websockets.connect", new=fake_connect),
        patch("app.bilibili.danmaku_ws.asyncio.sleep", new=_yield_sleep),
    ):
        from app.bilibili.danmaku_ws import DanmakuClient

        client = DanmakuClient(
            room_id=22210347,
            bili_client=bili,
            on_message=on_msg,
            _auth_timeout=2.0,
            _heartbeat_interval=999.0,
            _watchdog_timeout=999.0,
            _reconnect_delays=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        )
        ok = await asyncio.wait_for(client.start(), timeout=2.0)

    assert ok is True
    auth_frames = [s for s in fakes[0].sent if is_auth_frame(s)]
    assert len(auth_frames) == 1, fakes[0].sent
    assert auth_frame_proto_ver(auth_frames[0]) == proto.RAW, (
        f"AUTH frame protover must be RAW(1); got {auth_frame_proto_ver(auth_frames[0])}"
    )

    await asyncio.wait_for(client.stop(), timeout=6.0)


# ---------------------------------------------------------------------------
# Test 1c — DanmakuClient must accept and use a real-room-id passed by
# the parent (RoomSession.connect resolves short→real via bili_client).
# ---------------------------------------------------------------------------


async def test_start_uses_provided_real_room_id_in_auth_payload() -> None:
    """The AUTH body must carry the real room id the caller passed in.

    ccShield's DanmakuClient is constructed with the resolved room id
    (after ``resolve_room_id``), and its AUTH frame's ``roomid`` field
    uses that id. ``RoomSession.connect`` is responsible for resolving;
    this test pins the DanmakuClient contract on what it receives.
    """
    bili = make_bili_client(user_info={"mid": 1}, buvid3="device-id")
    captured_bodies: list[dict[str, Any]] = []
    fakes: list[FakeWebSocket] = []

    async def fake_connect(_url: str, **_kw: Any) -> FakeWebSocket:
        ws = FakeWebSocket(recv_frames=[pack_auth_rsp(0)])
        fakes.append(ws)
        return ws

    def grab_auth_body(raw: bytes) -> None:
        if not is_auth_frame(raw):
            return
        body = raw[16:]  # payload after 16-byte header
        captured_bodies.append(json.loads(body.decode("utf-8")))

    with (
        patch("app.bilibili.danmaku_ws.websockets.connect", new=fake_connect),
        patch("app.bilibili.danmaku_ws.asyncio.sleep", new=_yield_sleep),
    ):
        from app.bilibili.danmaku_ws import DanmakuClient

        client = DanmakuClient(
            room_id=22210347,  # the resolved real id, not a short id
            bili_client=bili,
            on_message=AsyncMock(),
            _auth_timeout=2.0,
            _heartbeat_interval=999.0,
            _watchdog_timeout=999.0,
            _reconnect_delays=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        )
        ok = await asyncio.wait_for(client.start(), timeout=2.0)

    assert ok is True
    for sent in fakes[0].sent:
        grab_auth_body(sent)

    assert len(captured_bodies) == 1
    auth_body = captured_bodies[0]
    assert auth_body["roomid"] == 22210347
    assert auth_body["uid"] == 1
    assert auth_body["protover"] == 3
    assert auth_body["platform"] == "web"
    assert auth_body["type"] == 2
    assert auth_body["key"] == "tok-abc"
    assert auth_body["buvid"] == "device-id"

    await asyncio.wait_for(client.stop(), timeout=6.0)


# ---------------------------------------------------------------------------
# Test 2 — auth failure is fatal (no retry, return False).
# ---------------------------------------------------------------------------
async def test_auth_failure_is_fatal_no_retry() -> None:
    """AUTH_RSP code != 0 → start() returns False, connect called ONCE."""
    bili = make_bili_client(user_info={"mid": 1})
    connect_calls: list[str] = []

    async def fake_connect(url: str, **_kw: Any) -> FakeWebSocket:
        connect_calls.append(url)
        # First recv yields a fatal AUTH_RSP; then disconnect.
        ws = FakeWebSocket(recv_frames=[pack_auth_rsp(-101)])
        return ws

    received: list[dict[str, Any]] = []

    async def on_msg(msg: dict[str, Any]) -> None:
        received.append(msg)

    with (
        patch("app.bilibili.danmaku_ws.websockets.connect", new=fake_connect),
        patch("app.bilibili.danmaku_ws.asyncio.sleep", new=_yield_sleep),
    ):
        from app.bilibili.danmaku_ws import DanmakuClient

        client = DanmakuClient(
            room_id=42,
            bili_client=bili,
            on_message=on_msg,
            _auth_timeout=2.0,
            _reconnect_delays=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            _reconnect_max_attempts=10,
        )
        ok = await asyncio.wait_for(client.start(), timeout=2.0)

    assert ok is False
    # Fatal auth → no reconnect. Exactly one connect call.
    assert len(connect_calls) == 1, connect_calls
    assert received == []


# ---------------------------------------------------------------------------
# Test 3 — heartbeat is sent shortly after auth.
# ---------------------------------------------------------------------------
async def test_heartbeat_frame_sent_after_auth() -> None:
    """After start(), FakeWS.sent contains a HEARTBEAT frame."""
    bili = make_bili_client(user_info={"mid": 9})
    fakes: list[FakeWebSocket] = []

    async def fake_connect(_url: str, **_kw: Any) -> FakeWebSocket:
        ws = FakeWebSocket(recv_frames=[pack_auth_rsp(0)])
        fakes.append(ws)
        return ws

    from app.bilibili.danmaku_ws import DanmakuClient

    client = DanmakuClient(
        room_id=1,
        bili_client=bili,
        on_message=AsyncMock(),
        _auth_timeout=2.0,
        _heartbeat_interval=0.01,  # 10 ms — heartbeat fires immediately
        _watchdog_timeout=999.0,
        _reconnect_delays=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        _reconnect_max_attempts=2,
    )

    with (
        patch("app.bilibili.danmaku_ws.websockets.connect", new=fake_connect),
        patch("app.bilibili.danmaku_ws.asyncio.sleep", new=_yield_sleep),
    ):
        ok = await asyncio.wait_for(client.start(), timeout=2.0)
        assert ok is True

        # Yield until at least one heartbeat frame appears.
        for _ in range(50):
            if any(is_heartbeat_frame(s) for s in fakes[0].sent):
                break
            await _real_sleep(0)

        sent_heartbeats = sum(1 for s in fakes[0].sent if is_heartbeat_frame(s))
        assert sent_heartbeats >= 1, fakes[0].sent

        await asyncio.wait_for(client.stop(), timeout=6.0)


# ---------------------------------------------------------------------------
# Test 4 — reconnect backoff: 6 attempts max, then gives up.
# ---------------------------------------------------------------------------
async def test_reconnect_after_disconnect_then_give_up() -> None:
    """FakeWS.recv raises ConnectionClosed immediately after auth →
    the connect loop reconnects. With _reconnect_delays=[0,0,0,0,0,0]
    and max 6 attempts, websockets.connect is called exactly 6 times.
    """
    bili = make_bili_client(user_info={"mid": 7})
    connect_calls: list[str] = []

    # Each connect returns the SAME fake — once its recv queue is empty,
    # the listen task disconnects and the loop reconnects.
    fake = FakeWebSocket(recv_frames=[pack_auth_rsp(0)])

    async def fake_connect(url: str, **_kw: Any) -> FakeWebSocket:
        connect_calls.append(url)
        return fake

    from app.bilibili.danmaku_ws import DanmakuClient

    client = DanmakuClient(
        room_id=5,
        bili_client=bili,
        on_message=AsyncMock(),
        _auth_timeout=2.0,
        _heartbeat_interval=999.0,
        _watchdog_timeout=999.0,
        _reconnect_delays=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        _reconnect_max_attempts=6,
    )

    with (
        patch("app.bilibili.danmaku_ws.websockets.connect", new=fake_connect),
        patch("app.bilibili.danmaku_ws.asyncio.sleep", new=_yield_sleep),
    ):
        ok = await asyncio.wait_for(client.start(), timeout=2.0)
        assert ok is True

        # Let the connect loop cycle through its 6 attempts.
        for _ in range(100):
            if len(connect_calls) >= 6:
                break
            await _real_sleep(0)

    assert len(connect_calls) == 6, connect_calls

    await asyncio.wait_for(client.stop(), timeout=6.0)


async def test_reconnect_rotates_upstream_endpoints() -> None:
    """Consecutive failures use every endpoint instead of pinning host 0."""
    bili = make_bili_client(user_info={"mid": 7})
    connect_calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_connect(url: str, **kwargs: Any) -> FakeWebSocket:
        connect_calls.append((url, kwargs))
        raise OSError("synthetic endpoint failure")

    from app.bilibili.danmaku_ws import DanmakuClient

    client = DanmakuClient(
        room_id=5,
        bili_client=bili,
        on_message=AsyncMock(),
        _reconnect_delays=(0.0,),
        _reconnect_max_attempts=5,
    )
    client.running = True

    urls = (
        "wss://chat0.bilibili.com:443/sub",
        "wss://chat1.bilibili.com:443/sub",
        "wss://chat2.bilibili.com:443/sub",
    )
    with (
        patch("app.bilibili.danmaku_ws.websockets.connect", new=fake_connect),
        patch("app.bilibili.danmaku_ws.asyncio.sleep", new=_yield_sleep),
    ):
        await client._connect_loop(urls)

    assert [url for url, _kwargs in connect_calls] == [
        urls[0],
        urls[1],
        urls[2],
        urls[0],
        urls[1],
    ]
    assert all(kwargs["max_queue"] == 256 for _url, kwargs in connect_calls)
    assert client.metrics_snapshot["connection_attempts"] == 5


# ---------------------------------------------------------------------------
# Test 5 — queue full → put_nowait drops with a warning (no crash).
# ---------------------------------------------------------------------------
async def test_queue_full_drops_messages_without_crashing() -> None:
    """Pre-fill the queue to maxsize. Next put_nowait from listen drops.

    An instant AsyncMock consumer would drain the queue as fast as the
    listen task fills it (so no drops ever happen). Use a slow consumer
    that yields for 10 ms per message — the queue (maxsize=4) fills up
    and the next ``put_nowait`` drops with a warning.
    """
    class HoldOpenFakeWebSocket(FakeWebSocket):
        """Keep the single mocked connection open after its frames drain."""

        def __init__(self, recv_frames: list[bytes]) -> None:
            super().__init__(recv_frames)
            self._closed_event = asyncio.Event()

        async def recv(self) -> bytes:
            if self.closed:
                raise ConnectionClosed(None, None)
            if self._recv:
                return self._recv.pop(0)
            await self._closed_event.wait()
            raise ConnectionClosed(None, None)

        async def close(self) -> None:
            await super().close()
            self._closed_event.set()

    bili = make_bili_client(user_info={"mid": 99})
    fakes: list[FakeWebSocket] = []

    async def fake_connect(_url: str, **_kw: Any) -> FakeWebSocket:
        ws = HoldOpenFakeWebSocket(
            recv_frames=[pack_auth_rsp(0)]
            + [pack_danmu_msg(dm_v2=f"m-{i}") for i in range(50)]
        )
        fakes.append(ws)
        return ws

    forwarded: list[dict[str, Any]] = []

    async def slow_then_track(msg: dict[str, Any]) -> None:
        forwarded.append(msg)
        await _real_sleep(0.01)

    from app.bilibili.danmaku_ws import DanmakuClient

    client = DanmakuClient(
        room_id=1,
        bili_client=bili,
        on_message=slow_then_track,
        _auth_timeout=2.0,
        _heartbeat_interval=999.0,
        _watchdog_timeout=999.0,
        _reconnect_delays=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        _reconnect_max_attempts=2,
        _queue_maxsize=4,
    )

    with (
        patch("app.bilibili.danmaku_ws.websockets.connect", new=fake_connect),
        patch("app.bilibili.danmaku_ws.asyncio.sleep", new=_yield_sleep),
    ):
        ok = await asyncio.wait_for(client.start(), timeout=2.0)
        assert ok is True

        for _ in range(500):
            await _real_sleep(0)

        await asyncio.wait_for(client.stop(), timeout=6.0)

    assert 0 < len(forwarded) < 50, len(forwarded)
    dropped_messages = client.metrics_snapshot["dropped_messages"]
    assert isinstance(dropped_messages, int)
    assert dropped_messages > 0


# ---------------------------------------------------------------------------
# Test 6 — stop() cleans up tasks, closes ws, clears queue.
# ---------------------------------------------------------------------------
async def test_stop_cleans_up_tasks_ws_and_queue() -> None:
    """After start(), stop() must complete within 5 s and close the ws."""
    bili = make_bili_client(user_info={"mid": 5})
    fakes: list[FakeWebSocket] = []

    async def fake_connect(_url: str, **_kw: Any) -> FakeWebSocket:
        ws = FakeWebSocket(recv_frames=[pack_auth_rsp(0)])
        fakes.append(ws)
        return ws

    from app.bilibili.danmaku_ws import DanmakuClient

    client = DanmakuClient(
        room_id=1,
        bili_client=bili,
        on_message=AsyncMock(),
        _auth_timeout=2.0,
        _heartbeat_interval=999.0,
        _watchdog_timeout=999.0,
        _reconnect_delays=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        _reconnect_max_attempts=2,
    )

    with (
        patch("app.bilibili.danmaku_ws.websockets.connect", new=fake_connect),
        patch("app.bilibili.danmaku_ws.asyncio.sleep", new=_yield_sleep),
    ):
        ok = await asyncio.wait_for(client.start(), timeout=2.0)
        assert ok is True

        await asyncio.wait_for(client.stop(), timeout=5.0)

    assert client.ws is None
    assert all(f.closed for f in fakes), [f.closed for f in fakes]
    assert client.msg_queue is not None
    assert client.msg_queue.empty()
