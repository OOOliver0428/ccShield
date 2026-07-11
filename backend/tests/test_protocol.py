"""Tests for the Bili live WS frame protocol parser.

TDD red-phase: these tests are written BEFORE backend/app/bilibili/protocol.py
exists. They MUST fail at collection (ModuleNotFoundError) and pass after
implementation lands.

Reference: ccShield/app/core/danmaku_ws.py _pack_data / _unpack_data (lines
123-270). The ccShield implementation uses a handwritten brace-matcher to split
multiple concatenated JSONs in a decompressed NORMAL payload — that pattern is
explicitly forbidden by the task spec. The replacement uses json.loads on the
whole payload; if that fails (because B站 nests 16-byte sub-frames inside
compressed payloads), it recurses into unpack_data.
"""

from __future__ import annotations

import json
import struct
import zlib

import brotli

from app.bilibili import protocol as proto


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
def test_header_length_constant() -> None:
    """Protocol header is fixed at 16 bytes."""
    assert proto.HEADER_LENGTH == 16


def test_opcode_constants() -> None:
    """Opcodes match the B站 live-WS protocol spec."""
    assert proto.HEARTBEAT == 2
    assert proto.HEARTBEAT_RSP == 3
    assert proto.NORMAL == 5
    assert proto.AUTH == 7
    assert proto.AUTH_RSP == 8


def test_proto_version_constants() -> None:
    """Proto versions match the spec: plain=0, raw=1, zlib=2, brotli=3."""
    assert proto.PLAIN == 0
    assert proto.RAW == 1
    assert proto.ZLIB == 2
    assert proto.BROTLI == 3


# --------------------------------------------------------------------------- #
# Test 1 — pack/unpack round-trip on a single NORMAL/JSON packet.
# --------------------------------------------------------------------------- #
def test_pack_unpack_roundtrip_normal_raw() -> None:
    payload = {"cmd": "DANMU_MSG", "info": [1, "hi", [101, "alice", 0, 0]]}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    packet = proto.pack_data(raw, proto.NORMAL)

    assert len(packet) == 16 + len(raw)
    _total_len, header_len, proto_ver, packet_type, _seq = struct.unpack(
        ">IHHII", packet[:16]
    )
    assert header_len == 16
    assert proto_ver == proto.RAW
    assert packet_type == proto.NORMAL

    msgs = proto.unpack_data(packet)
    assert msgs == [payload]


# --------------------------------------------------------------------------- #
# Test 2 — three NORMAL packets concatenated in a single TCP frame.
# --------------------------------------------------------------------------- #
def test_unpack_multiple_packets_in_stream() -> None:
    a = {"cmd": "DANMU_MSG", "info": ["a"]}
    b = {"cmd": "SEND_GIFT", "data": {"id": 1}}
    c = {"cmd": "INTERACT_WORD", "data": {"uid": 42}}
    stream = b"".join(
        proto.pack_data(json.dumps(x, separators=(",", ":")).encode("utf-8"), proto.NORMAL)
        for x in (a, b, c)
    )
    msgs = proto.unpack_data(stream)
    assert msgs == [a, b, c]


# --------------------------------------------------------------------------- #
# Test 3 — proto_ver=2 (zlib).
# --------------------------------------------------------------------------- #
def test_unpack_zlib_compressed_normal() -> None:
    payload = {"cmd": "DANMU_MSG", "info": [1.5, "压缩", {"nested": True}]}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    compressed = zlib.compress(raw)
    packet = proto.pack_data(compressed, proto.NORMAL, proto_ver=proto.ZLIB)

    msgs = proto.unpack_data(packet)
    assert msgs == [payload]


# --------------------------------------------------------------------------- #
# Test 4 — proto_ver=3 (brotli).
# --------------------------------------------------------------------------- #
def test_unpack_brotli_compressed_normal() -> None:
    payload = {"cmd": "DANMU_MSG", "info": ["brotli"], "extra": list(range(20))}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    compressed = brotli.compress(raw)
    packet = proto.pack_data(compressed, proto.NORMAL, proto_ver=proto.BROTLI)

    msgs = proto.unpack_data(packet)
    assert msgs == [payload]


# --------------------------------------------------------------------------- #
# Test 5 — AUTH_RSP with JSON body.
# --------------------------------------------------------------------------- #
def test_unpack_auth_rsp_returns_dict() -> None:
    body = {"code": 0}
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    packet = proto.pack_data(raw, proto.AUTH_RSP)
    msgs = proto.unpack_data(packet)
    # The raw parsed JSON is returned (consistent with NORMAL handler).
    assert msgs == [body]


# --------------------------------------------------------------------------- #
# Test 6 — HEARTBEAT_RSP with a 4-byte big-endian online count.
# --------------------------------------------------------------------------- #
def test_unpack_heartbeat_rsp_returns_online_count() -> None:
    payload = struct.pack(">I", 42)
    packet = proto.pack_data(payload, proto.HEARTBEAT_RSP)
    msgs = proto.unpack_data(packet)
    assert msgs == [{"online_count": 42}]


def test_unpack_heartbeat_rsp_large_count() -> None:
    payload = struct.pack(">I", 0xDEADBEEF)
    packet = proto.pack_data(payload, proto.HEARTBEAT_RSP)
    msgs = proto.unpack_data(packet)
    assert msgs == [{"online_count": 0xDEADBEEF}]


# --------------------------------------------------------------------------- #
# Test 7 — Truncation: bytes shorter than the 16-byte header.
# --------------------------------------------------------------------------- #
def test_unpack_truncated_returns_empty() -> None:
    # 10 bytes is less than HEADER_LENGTH (16) — must not crash.
    msgs = proto.unpack_data(b"\x00" * 10)
    assert msgs == []


# --------------------------------------------------------------------------- #
# Test 8 — Nested sub-frames: a brotli-compressed payload that itself
# contains two NORMAL sub-frames must yield both messages after recursion.
# --------------------------------------------------------------------------- #
def test_unpack_nested_subframes_inside_brotli() -> None:
    inner_a = {"cmd": "DANMU_MSG", "info": ["a"]}
    inner_b = {"cmd": "DANMU_MSG", "info": ["b"]}
    inner_stream = b"".join(
        proto.pack_data(json.dumps(x, separators=(",", ":")).encode("utf-8"), proto.NORMAL)
        for x in (inner_a, inner_b)
    )
    compressed = brotli.compress(inner_stream)
    outer = proto.pack_data(compressed, proto.NORMAL, proto_ver=proto.BROTLI)

    msgs = proto.unpack_data(outer)
    assert msgs == [inner_a, inner_b]


def test_unpack_brotli_with_plain_v0_normal_subframes() -> None:
    """Real Brotli payloads commonly contain NORMAL JSON frames at version 0."""
    inner = {"cmd": "DANMU_MSG", "info": [1.0, "live-shape", [7, "alice"]]}
    raw = json.dumps(inner, separators=(",", ":")).encode("utf-8")
    inner_frame = proto.pack_data(raw, proto.NORMAL, proto_ver=proto.PLAIN)
    outer = proto.pack_data(
        brotli.compress(inner_frame), proto.NORMAL, proto_ver=proto.BROTLI
    )

    assert proto.unpack_data(outer) == [inner]


def test_unpack_nested_subframes_inside_zlib() -> None:
    """Same recursion path for proto_ver=2."""
    inner_a = {"cmd": "SEND_GIFT", "data": {"id": 1}}
    inner_b = {"cmd": "INTERACT_WORD", "data": {"uid": 7}}
    inner_stream = b"".join(
        proto.pack_data(json.dumps(x, separators=(",", ":")).encode("utf-8"), proto.NORMAL)
        for x in (inner_a, inner_b)
    )
    compressed = zlib.compress(inner_stream)
    outer = proto.pack_data(compressed, proto.NORMAL, proto_ver=proto.ZLIB)

    msgs = proto.unpack_data(outer)
    assert msgs == [inner_a, inner_b]


# --------------------------------------------------------------------------- #
# Adversarial / edge-case coverage — pushes branch coverage above the 80%
# floor and locks in the no-crash / graceful-skip behaviour.
# --------------------------------------------------------------------------- #
def test_pack_data_default_proto_ver_is_raw() -> None:
    """pack_data defaults to proto_ver=RAW (=1)."""
    packet = proto.pack_data(b"x", proto.NORMAL)
    _, _, proto_ver, _, _ = struct.unpack(">IHHII", packet[:16])
    assert proto_ver == proto.RAW


def test_unpack_empty_bytes_returns_empty() -> None:
    assert proto.unpack_data(b"") == []


def test_unpack_heartbeat_outgoing_produces_no_message() -> None:
    """A packet we SEND (HEARTBEAT, type=2) carries no incoming payload."""
    packet = proto.pack_data(b'[object Object]', proto.HEARTBEAT)
    assert proto.unpack_data(packet) == []


def test_unpack_auth_outgoing_produces_no_message() -> None:
    """A packet we SEND (AUTH, type=7) carries no incoming payload."""
    packet = proto.pack_data(b"{}", proto.AUTH)
    assert proto.unpack_data(packet) == []


def test_unpack_malformed_header_len_aborts() -> None:
    """A header whose header_len field is not 16 must not be parsed."""
    # 16 bytes with header_len=99 instead of 16.
    bad_header = struct.pack(">IHHII", 16, 99, 1, proto.NORMAL, 1)
    assert proto.unpack_data(bad_header) == []


def test_unpack_malformed_total_len_aborts() -> None:
    """A header whose total_len < HEADER_LENGTH must not be parsed."""
    bad_header = struct.pack(">IHHII", 4, 16, 1, proto.NORMAL, 1)
    assert proto.unpack_data(bad_header) == []


def test_unpack_truncated_packet_body_returns_partial() -> None:
    """If the declared total_len exceeds available data, return what's parsed."""
    payload = {"cmd": "OK"}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    packet = proto.pack_data(raw, proto.NORMAL)
    # Drop the last 5 bytes to simulate a truncated TCP read.
    msgs = proto.unpack_data(packet[:-5])
    assert msgs == []


def test_unpack_zlib_garbage_skips_packet_then_continues() -> None:
    """A NORMAL packet whose payload fails zlib decompress must not crash;
    the next valid packet in the stream must still be returned."""
    body = {"cmd": "OK"}
    valid = proto.pack_data(
        json.dumps(body, separators=(",", ":")).encode("utf-8"), proto.NORMAL
    )
    # Garbage zlib payload, with a header that claims it's zlib-compressed.
    garbage = proto.pack_data(b"\xff\xff\xff\xff\xff", proto.NORMAL, proto_ver=proto.ZLIB)
    msgs = proto.unpack_data(garbage + valid)
    assert msgs == [body]


def test_unpack_brotli_garbage_skips_packet_then_continues() -> None:
    """Same for brotli: invalid brotli payload must skip, not raise."""
    body = {"cmd": "OK"}
    valid = proto.pack_data(
        json.dumps(body, separators=(",", ":")).encode("utf-8"), proto.NORMAL
    )
    garbage = proto.pack_data(b"\xff\xff\xff\xff\xff", proto.NORMAL, proto_ver=proto.BROTLI)
    msgs = proto.unpack_data(garbage + valid)
    assert msgs == [body]


def test_unpack_normal_payload_invalid_json_returns_empty() -> None:
    """A NORMAL packet whose payload is neither JSON nor sub-frames yields nothing."""
    # Raw proto_ver=1, payload is garbage.
    packet = proto.pack_data(b"this is not json {", proto.NORMAL)
    assert proto.unpack_data(packet) == []


def test_unpack_normal_payload_trailing_garbage_after_subframes() -> None:
    """Trailing bytes after the last sub-frame are tolerated (B站 may pad)."""
    a = {"cmd": "A"}
    inner = proto.pack_data(json.dumps(a, separators=(",", ":")).encode("utf-8"), proto.NORMAL)
    # Append 3 bytes of garbage after the valid inner packet.
    payload = inner + b"\x00\x00\x00"
    packet = proto.pack_data(payload, proto.NORMAL)
    msgs = proto.unpack_data(packet)
    assert msgs == [a]


def test_unpack_auth_rsp_invalid_json_is_skipped() -> None:
    """AUTH_RSP with non-JSON payload must not crash and must skip the message."""
    packet = proto.pack_data(b"not json", proto.AUTH_RSP)
    assert proto.unpack_data(packet) == []


def test_unpack_heartbeat_rsp_short_payload_skipped() -> None:
    """HEARTBEAT_RSP with fewer than 4 bytes must not crash."""
    packet = proto.pack_data(b"\x00\x00", proto.HEARTBEAT_RSP)
    assert proto.unpack_data(packet) == []


def test_unpack_unknown_proto_ver_skips_packet() -> None:
    """A packet with an unknown proto version (not 0/1/2/3) is skipped."""
    body = {"cmd": "OK"}
    valid = proto.pack_data(
        json.dumps(body, separators=(",", ":")).encode("utf-8"), proto.NORMAL
    )
    unknown = proto.pack_data(b"\xff", proto.NORMAL, proto_ver=99)
    msgs = proto.unpack_data(unknown + valid)
    assert msgs == [body]


def test_unpack_auth_rsp_non_dict_json_is_skipped() -> None:
    """AUTH_RSP whose JSON decodes to a non-object (e.g., a list) is skipped."""
    packet = proto.pack_data(b"[1, 2, 3]", proto.AUTH_RSP)
    assert proto.unpack_data(packet) == []
