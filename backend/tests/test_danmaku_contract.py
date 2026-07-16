"""Contract tests for the B站 to typed-BridgeEvent pipeline.

These tests PIN the shape mapping from a B站 live-WS frame to the typed
:class:`~app.room.events.BridgeEvent` produced by
:meth:`~app.room.session.RoomSession._normalize`. They are SYNTHETIC:
the frames are constructed inline with :func:`app.bilibili.protocol.pack_data`
based on the field shapes documented in ``ccShield/app/core/danmaku_ws.py``
(lines 401-431 for DANMU_MSG, 451-472 for SUPER_CHAT_MESSAGE).

No real Cookie / network / captured fixtures are needed at this stage.
Real-fixture capture is deferred to an explicit manual gate, where the synthetic
shapes here will be cross-validated against actual B站 captures.

Scenarios covered:

  1. NORMAL/JSON frame → DANMU_MSG (uid, uname, text, ts, guard_level,
     medal). Exercises bytes → unpack → _normalize → DanmakuEvent.
  2. BROTLI-compressed NORMAL frame → SUPER_CHAT_MESSAGE. Exercises
     brotli decompress → unpack → _normalize → SuperChatEvent.
  3. Multi-packet stream: two DANMU_MSG frames concatenated in a single
     TCP frame. Exercises unpack of multiple back-to-back packets.
  4. AUTH_RSP frame (``{"code":0}``). Confirms protocol-layer messages
     do NOT leak through as BridgeEvents (no ``cmd`` → ``_normalize``
     returns ``None``).
  5. Full pipeline: bytes → unpack → ``_on_raw_message`` → registered
     callback. End-to-end smoke test through the session layer.
  6. Dedup: same DANMU_MSG (same ``dm_v2`` id) fed twice through
     ``_on_raw_message`` → callback invoked once. Confirms the bounded
     seen-msg-id ring buffer holds across synthetic frames.

Reference shapes (ccShield ``app/core/danmaku_ws.py``):

* DANMU_MSG — ``info[1]=text``, ``info[2][0]=uid``, ``info[2][1]=uname``,
  ``info[0][4]=ts``, ``info[3]=[name,level]``, ``info[7]=guard_level``.
* SUPER_CHAT_MESSAGE — ``data.uid``, ``data.user_info.uname``,
  ``data.message``, ``data.price``, ``data.start_time``.
"""
from __future__ import annotations

import json
from typing import cast

import brotli

from app.bilibili import protocol as proto
from app.bilibili.client import BilibiliClient
from app.room.events import (
    BridgeEvent,
    DanmakuEvent,
    Medal,
    SuperChatEvent,
)
from app.room.session import RoomSession

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_bili() -> BilibiliClient:
    """Cheap stand-in: RoomSession stores it; tests never call its methods.

    Mirrors the ``cast``-based stub in ``test_room_session.py`` so the
    type checker sees ``BilibiliClient`` without importing the heavy
    client module's full surface.
    """
    return cast("BilibiliClient", object())


def _pack_normal_json(payload: dict[str, object]) -> bytes:
    """Wrap ``payload`` as a NORMAL frame with proto_ver=1 (raw JSON)."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return proto.pack_data(body, proto.NORMAL, proto_ver=proto.RAW)


def _pack_normal_brotli(payload: dict[str, object]) -> bytes:
    """Wrap ``payload`` as a NORMAL frame with proto_ver=3 (brotli)."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    compressed = brotli.compress(body)
    return proto.pack_data(compressed, proto.NORMAL, proto_ver=proto.BROTLI)


def _pack_auth_rsp(body: dict[str, object]) -> bytes:
    """Wrap ``body`` as an AUTH_RSP frame (proto_ver=1)."""
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    return proto.pack_data(raw, proto.AUTH_RSP, proto_ver=proto.RAW)


# ---------------------------------------------------------------------------
# Synthetic B站 payloads — based on ccShield field shapes.
# ---------------------------------------------------------------------------

# DANMU_MSG info array, 8 elements (indices 0..7) matching the standard
# B站 layout so RoomSession._normalize extracts every field:
#   info[0]    — sender meta + ts at [4]
#   info[1]    — text
#   info[2]    — [uid, uname, is_admin, is_vip, ...]
#   info[3]    — [medal_level, medal_name, anchor_uname, ...] (B站 order)
#   info[4..6] — B站-specific extras (room flags etc.)
#   info[7]    — guard_level (0=none, 1=总督, 2=提督, 3=舰长)
_DANMU_INFO_FULL: list[object] = [
    [0, 0, 0, 0, 1700000000, 0],  # info[0][4] = 1700000000 (ts)
    "hello world",                  # info[1] = text
    [123, "alice", 0, 0, 0],        # info[2][0] = uid, info[2][1] = uname
    [5, "粉丝团", "anchor1"],          # info[3] = [level, name, anchor]
    [],                             # info[4] — empty list (B站 default)
    0,                              # info[5]
    0,                              # info[6]
    3,                              # info[7] = guard_level (3 = 舰长)
]

DANMU_MSG_PAYLOAD: dict[str, object] = {
    "cmd": "DANMU_MSG",
    "info": _DANMU_INFO_FULL,
}

SUPER_CHAT_PAYLOAD: dict[str, object] = {
    "cmd": "SUPER_CHAT_MESSAGE",
    "data": {
        "id": 7788,
        "uid": 456,
        "user_info": {"uname": "bob", "guard_level": 3},
        "message": "support",
        "price": 50,
        "start_time": 1700000001,
        "end_time": 1700000121,
        "time": 120,
        "background_color": "#EDF5FF",
        "background_bottom_color": "#2A60B2",
        "background_price_color": "#7497CD",
        "message_font_color": "#24476B",
    },
}

AUTH_RSP_PAYLOAD: dict[str, object] = {"code": 0}


# ---------------------------------------------------------------------------
# Scenario 1 — NORMAL/JSON frame → DANMU_MSG → DanmakuEvent
# ---------------------------------------------------------------------------


def test_contract_normal_json_danmu_msg() -> None:
    """A NORMAL/JSON frame whose payload is a DANMU_MSG normalizes to a
    :class:`DanmakuEvent` with all fields mapped from the live B站 layout.

    This is the canonical DANMU_MSG shape B站 actually sends — guard_level
    sits at info[7], medal at info[3]=[level, name, ...] (level first),
    ts at info[0][4]. If B站 changes any of these field positions, this
    test fails, prompting a re-capture / contract revision.
    """
    frame = _pack_normal_json(DANMU_MSG_PAYLOAD)

    # Protocol layer: bytes → one raw dict.
    raws = proto.unpack_data(frame)
    assert len(raws) == 1
    raw = raws[0]
    assert raw["cmd"] == "DANMU_MSG"

    # Normalization layer: raw dict → typed DanmakuEvent.
    sess = RoomSession(bili_client=_stub_bili())
    event = sess._normalize(raw)

    assert isinstance(event, DanmakuEvent)
    assert event.type == "danmaku"
    assert event.uid == 123
    assert event.uname == "alice"
    assert event.text == "hello world"
    assert event.ts == 1700000000
    assert event.guard_level == 3  # 舰长
    assert isinstance(event.medal, Medal)
    assert event.medal.name == "粉丝团"
    assert event.medal.level == 5


# ---------------------------------------------------------------------------
# Scenario 2 — BROTLI-compressed NORMAL → SUPER_CHAT_MESSAGE → SuperChatEvent
# ---------------------------------------------------------------------------


def test_contract_brotli_super_chat_message() -> None:
    """A brotli-compressed NORMAL frame whose payload is a
    SUPER_CHAT_MESSAGE normalizes to a :class:`SuperChatEvent`.

    Pins the field map:
      data.uid           → uid
      data.user_info.uname → uname
      data.message       → text
      data.price         → price
      data.start_time    → ts
    """
    frame = _pack_normal_brotli(SUPER_CHAT_PAYLOAD)

    # Brotli-decompress path is exercised here — unpack_data must
    # transparently hand the decompressed JSON to _parse_normal_payload.
    raws = proto.unpack_data(frame)
    assert len(raws) == 1
    raw = raws[0]
    assert raw["cmd"] == "SUPER_CHAT_MESSAGE"

    sess = RoomSession(bili_client=_stub_bili())
    event = sess._normalize(raw)

    assert isinstance(event, SuperChatEvent)
    assert event.type == "sc"
    assert event.uid == 456
    assert event.uname == "bob"
    assert event.text == "support"
    assert event.price == 50
    assert event.ts == 1700000001
    assert event.id == "7788"
    assert event.end_ts == 1700000121
    assert event.duration == 120
    assert event.guard_level == 3


# ---------------------------------------------------------------------------
# Scenario 3 — Multi-packet stream: 2 DANMU_MSG frames concatenated.
# ---------------------------------------------------------------------------


def test_contract_multi_packet_stream_two_danmu_msgs() -> None:
    """Two NORMAL/JSON frames concatenated in a single TCP frame.

    B站 commonly batches multiple DANMU_MSGs inside one TCP read.
    unpack_data must yield both raw dicts in order; each one must
    normalize independently.
    """
    a_payload: dict[str, object] = {
        "cmd": "DANMU_MSG",
        "info": [
            [0, 0, 0, 0, 1700000010, 0],
            "first",
            [100, "user_a", 0, 0, 0],
        ],
    }
    b_payload: dict[str, object] = {
        "cmd": "DANMU_MSG",
        "info": [
            [0, 0, 0, 0, 1700000020, 0],
            "second",
            [200, "user_b", 0, 0, 0],
        ],
    }

    stream = _pack_normal_json(a_payload) + _pack_normal_json(b_payload)
    raws = proto.unpack_data(stream)
    assert len(raws) == 2
    assert raws[0]["cmd"] == "DANMU_MSG"
    assert raws[1]["cmd"] == "DANMU_MSG"

    sess = RoomSession(bili_client=_stub_bili())
    e1 = sess._normalize(raws[0])
    e2 = sess._normalize(raws[1])

    assert isinstance(e1, DanmakuEvent)
    assert e1.text == "first"
    assert e1.uid == 100
    assert e1.uname == "user_a"
    assert e1.ts == 1700000010
    assert e1.medal is None  # info[3] absent → no medal

    assert isinstance(e2, DanmakuEvent)
    assert e2.text == "second"
    assert e2.uid == 200
    assert e2.uname == "user_b"
    assert e2.ts == 1700000020
    assert e2.medal is None


# ---------------------------------------------------------------------------
# Scenario 4 — AUTH_RSP frame is protocol-layer, not a BridgeEvent.
# ---------------------------------------------------------------------------


def test_contract_auth_rsp_does_not_become_bridge_event() -> None:
    """An AUTH_RSP ``{"code":0}`` is correctly decoded at the protocol
    layer but is NOT forwarded as a :class:`BridgeEvent` — it has no
    ``cmd`` field, so :meth:`RoomSession._normalize` returns ``None``.

    This locks in the boundary between the wire protocol and the
    normalized event stream: protocol-layer messages stay protocol.
    """
    frame = _pack_auth_rsp(AUTH_RSP_PAYLOAD)

    raws = proto.unpack_data(frame)
    assert len(raws) == 1
    assert raws[0] == AUTH_RSP_PAYLOAD
    # Crucially, no "cmd" field — AUTH_RSP is not a DANMU_MSG / SC.
    assert "cmd" not in raws[0]

    sess = RoomSession(bili_client=_stub_bili())
    assert sess._normalize(raws[0]) is None


# ---------------------------------------------------------------------------
# Scenario 5 — Full pipeline: bytes → unpack → _on_raw_message → callback.
# ---------------------------------------------------------------------------


async def test_contract_full_pipeline_brotli_to_callback() -> None:
    """End-to-end: synthetic frame bytes go through the entire pipeline
    (``unpack_data`` → ``_on_raw_message`` → registered callback) and the
    callback receives the typed :class:`SuperChatEvent`.

    This is the integration surface that the FastAPI/WebSocket bridge will
    rely on: synthetic frames in, typed events out, no raw ``cmd``/``info``
    dict ever leaks past ``_normalize``.
    """
    sess = RoomSession(bili_client=_stub_bili())
    received: list[BridgeEvent] = []

    async def cb(event: BridgeEvent) -> None:
        received.append(event)

    await sess.add_callback(cb)

    frame = _pack_normal_brotli(SUPER_CHAT_PAYLOAD)
    raws = proto.unpack_data(frame)

    for raw in raws:
        await sess._on_raw_message(raw)

    assert len(received) == 1
    event = received[0]
    assert isinstance(event, SuperChatEvent)
    assert event.uid == 456
    assert event.uname == "bob"
    assert event.text == "support"
    assert event.price == 50
    assert event.ts == 1700000001


async def test_contract_full_pipeline_normal_json_to_callback() -> None:
    """Same end-to-end check for the raw-JSON DANMU_MSG path — guards
    against any regression where the brotli path works but raw-JSON
    silently drops.
    """
    sess = RoomSession(bili_client=_stub_bili())
    received: list[BridgeEvent] = []

    async def cb(event: BridgeEvent) -> None:
        received.append(event)

    await sess.add_callback(cb)

    frame = _pack_normal_json(DANMU_MSG_PAYLOAD)
    raws = proto.unpack_data(frame)

    for raw in raws:
        await sess._on_raw_message(raw)

    assert len(received) == 1
    event = received[0]
    assert isinstance(event, DanmakuEvent)
    assert event.text == "hello world"
    assert event.uid == 123
    assert event.guard_level == 3
    assert isinstance(event.medal, Medal)
    assert event.medal.name == "粉丝团"
    assert event.medal.level == 5


# ---------------------------------------------------------------------------
# Scenario 6 — Dedup via dm_v2 across synthetic frames.
# ---------------------------------------------------------------------------


async def test_contract_dedup_same_dm_v2_across_synthetic_frames() -> None:
    """Same DANMU_MSG (same ``dm_v2`` id) constructed from a synthetic
    frame, fed twice through ``_on_raw_message``, must trigger the
    callback exactly once.

    B站 repeats DANMU_MSG across multiple server-side fan-outs; the
    bounded ``_seen_ids`` ring buffer in RoomSession drops the second
    copy. This test pins that contract through the full synthetic
    pipeline (not just direct dict calls).
    """
    payload_with_id: dict[str, object] = {
        "cmd": "DANMU_MSG",
        "dm_v2": "synthetic-dedup-1",
        "info": [
            [0, 0, 0, 0, 1700000000, 0],
            "hello world",
            [123, "alice", 0, 0, 0],
            [5, "粉丝团", "anchor1"],
            [],
            0,
            0,
            3,
        ],
    }

    frame = _pack_normal_json(payload_with_id)
    raws = proto.unpack_data(frame)
    assert len(raws) == 1

    sess = RoomSession(bili_client=_stub_bili())
    received: list[BridgeEvent] = []

    async def cb(event: BridgeEvent) -> None:
        received.append(event)

    await sess.add_callback(cb)

    # Feed the SAME raw dict twice — simulates B站's duplicate fan-out.
    await sess._on_raw_message(raws[0])
    await sess._on_raw_message(raws[0])

    assert len(received) == 1
    assert isinstance(received[0], DanmakuEvent)
    assert received[0].text == "hello world"
    assert received[0].uid == 123


async def test_contract_dedup_different_dm_v2_both_forwarded() -> None:
    """Sanity-check the inverse: two DANMU_MSGs with DIFFERENT ``dm_v2``
    ids must both reach the callback. Guards against an overly-aggressive
    dedup that drops everything after the first id.
    """
    payload_a: dict[str, object] = {
        "cmd": "DANMU_MSG",
        "dm_v2": "id-A",
        "info": [
            [0, 0, 0, 0, 1700000000, 0],
            "msg-a",
            [1, "u-a", 0, 0, 0],
        ],
    }
    payload_b: dict[str, object] = {
        "cmd": "DANMU_MSG",
        "dm_v2": "id-B",
        "info": [
            [0, 0, 0, 0, 1700000001, 0],
            "msg-b",
            [2, "u-b", 0, 0, 0],
        ],
    }

    frame_a = _pack_normal_json(payload_a)
    frame_b = _pack_normal_json(payload_b)

    sess = RoomSession(bili_client=_stub_bili())
    received: list[BridgeEvent] = []

    async def cb(event: BridgeEvent) -> None:
        received.append(event)

    await sess.add_callback(cb)

    for raw in proto.unpack_data(frame_a):
        await sess._on_raw_message(raw)
    for raw in proto.unpack_data(frame_b):
        await sess._on_raw_message(raw)

    assert len(received) == 2
    danmu_texts = {e.text for e in received if isinstance(e, DanmakuEvent)}
    assert danmu_texts == {"msg-a", "msg-b"}
