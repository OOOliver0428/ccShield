"""Bili live-WS frame pack / unpack primitives.

Header (16 bytes, big-endian)::

    >IHHII  total_length  header_length=16  proto_ver  packet_type  sequence=1

- proto_ver: 1=raw JSON, 2=zlib, 3=brotli.
- packet_type: 2=HEARTBEAT, 3=HEARTBEAT_RSP, 5=NORMAL, 7=AUTH, 8=AUTH_RSP.
- A single TCP frame may contain MULTIPLE packets concatenated. A single
  decompressed NORMAL payload may also contain MULTIPLE nested sub-frames
  (B站 re-frames the payload with the same 16-byte header).

The implementation replaces ccShield's handwritten brace-matcher
(danmaku_ws.py:199-244) with json.loads on the whole payload; if that
fails (because the payload is itself a stream of nested sub-frames),
we recurse into :func:`unpack_data`.
"""

from __future__ import annotations

import json
import struct
import zlib

import brotli

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
HEADER_LENGTH: int = 16
SEQUENCE: int = 1

# Opcodes (B站 protocol spec).
HEARTBEAT: int = 2
HEARTBEAT_RSP: int = 3
NORMAL: int = 5
AUTH: int = 7
AUTH_RSP: int = 8

# Protocol versions.
RAW: int = 1
ZLIB: int = 2
BROTLI: int = 3

_HEADER_STRUCT: struct.Struct = struct.Struct(">IHHII")


# --------------------------------------------------------------------------- #
# pack
# --------------------------------------------------------------------------- #
def pack_data(data: bytes, packet_type: int, proto_ver: int = RAW) -> bytes:
    """Wrap ``data`` with a Bili 16-byte header.

    Args:
        data: Payload bytes (already encoded JSON / already compressed).
        packet_type: One of HEARTBEAT / HEARTBEAT_RSP / NORMAL / AUTH / AUTH_RSP.
        proto_ver: One of RAW / ZLIB / BROTLI. Defaults to RAW.

    Returns:
        header (16 bytes) + payload.
    """
    header = _HEADER_STRUCT.pack(
        len(data) + HEADER_LENGTH,
        HEADER_LENGTH,
        proto_ver,
        packet_type,
        SEQUENCE,
    )
    return header + data


# --------------------------------------------------------------------------- #
# unpack
# --------------------------------------------------------------------------- #
def unpack_data(data: bytes) -> list[dict]:
    """Parse all packets in ``data`` and return their messages.

    Behaviour:

    - Multiple packets may be concatenated in one TCP frame.
    - NORMAL packets are decompressed (zlib / brotli) before parsing.
    - The decompressed payload is first tried as a single JSON document. If
      that fails, the payload is treated as a stream of nested sub-frames and
      :func:`unpack_data` recurses on it.
    - AUTH_RSP payloads are parsed as JSON and returned as a single dict.
    - HEARTBEAT_RSP payloads are read as a 4-byte big-endian unsigned int
      (online count) and returned as ``{"online_count": N}``.
    - Truncated or malformed bytes are skipped gracefully — never crash.
    """
    messages: list[dict] = []
    offset = 0
    data_len = len(data)

    while offset + HEADER_LENGTH <= data_len:
        total_len, header_len, proto_ver, packet_type, _seq = _HEADER_STRUCT.unpack_from(
            data, offset
        )

        # Sanity-check the header. Any malformed value stops the parse;
        # callers get whatever was successfully decoded so far.
        if header_len != HEADER_LENGTH or total_len < HEADER_LENGTH:
            break
        if offset + total_len > data_len:
            # Truncated packet — wait for the rest.
            break

        payload = data[offset + header_len : offset + total_len]
        next_offset = offset + total_len

        # Decompress if the proto version says so. Decompression failures
        # are recoverable — we skip this packet and try the next one.
        if proto_ver == ZLIB:
            try:
                payload = zlib.decompress(payload)
            except zlib.error:
                offset = next_offset
                continue
        elif proto_ver == BROTLI:
            try:
                payload = brotli.decompress(payload)
            except brotli.error:
                offset = next_offset
                continue
        elif proto_ver != RAW:
            # Unknown proto version — be conservative and skip the packet.
            offset = next_offset
            continue

        # Dispatch by packet type.
        if packet_type == NORMAL:
            messages.extend(_parse_normal_payload(payload))
        elif packet_type == AUTH_RSP:
            parsed = _parse_json(payload)
            if parsed is not None:
                messages.append(parsed)
        elif packet_type == HEARTBEAT_RSP and len(payload) >= 4:
            (online_count,) = struct.unpack(">I", payload[:4])
            messages.append({"online_count": online_count})
        # HEARTBEAT (outgoing, type=2) and AUTH (outgoing, type=7) carry no
        # incoming payload — nothing to extract, just advance.

        offset = next_offset

    return messages


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _parse_json(payload: bytes) -> dict | None:
    """Decode ``payload`` as UTF-8 JSON; return ``None`` on failure."""
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded


def _parse_normal_payload(payload: bytes) -> list[dict]:
    """Parse a decompressed NORMAL packet payload.

    Strategy:
        1. Try to parse the whole payload as a single JSON document.
        2. If that fails, treat the payload as a stream of nested 16-byte
           sub-frames (B站 re-frames compressed payloads) and recurse.
    """
    parsed = _parse_json(payload)
    if parsed is not None:
        return [parsed]
    return unpack_data(payload)
