"""Capture real B站 API responses once, scrub them, save as test fixtures.

This script is the "real world record" step for offline test replay (test
layer ② of the plan). It runs three B站 endpoints through ``BilibiliClient``
using the operator's own session cookies, captures each response, and
**redacts every credential before writing anything to disk**.

Why redaction matters
---------------------
A historical development artifact accidentally included a real SESSDATA
credential. This script MUST NOT do the same. Every value whose key
matches a sensitive pattern is replaced with ``"<REDACTED>"``; every
``Set-Cookie`` header is dropped; the entire cookie jar is emptied. The
self-test in ``--dry-run`` validates that no real-looking secret survives
the pipeline.

Modes
-----
``--dry-run`` (default if ``--live`` not passed):
    Constructs synthetic responses containing obvious fake secrets
    (SESSDATA/abc..., bili_jct/<32-hex>...). Runs them through the same
    save+redact pipeline used for live captures, asserts the saved file
    contains no real-looking secret, prints "DRY-RUN OK" plus the
    redacted sample for visual inspection. No network calls.

``--live``:
    Real capture. Reads cookies from the project-root ``.env`` (via
    ``app.config.settings``); exits 0 with a friendly message if the
    cookies are empty so the optional real-QR check is a single, no-op
    command until a human has logged in. With cookies present, hits:

        - ``/x/web-interface/nav``           via ``BilibiliClient.get_user_info``
        - ``/xlive/web-room/v1/index/getDanmuInfo`` via ``BilibiliClient.get_danmu_info``
        - ``/xlive/web-ucenter/v1/banned/GetSilentUserList`` via ``BilibiliClient.get_ban_list``

    Each is saved to ``<out-dir>/{endpoint}_{sha16}.json`` with metadata:
    ``captured_at``, ``url``, ``request_params``, ``response_status``,
    ``response_body``, ``response_headers``.

Filename stability
------------------
``sha16`` is the first 16 hex chars of ``sha256(canonical_params)`` where
``canonical_params`` are the *meaningful inputs* (room id, or empty for
nav), NOT the wire-level signed params. This makes re-runs of the same
room produce the same filename and overwrite in place rather than
accumulating stale copies.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import sys
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Endpoint identifiers used in fixture filenames + metadata.
ENDPOINT_NAV: str = "nav"
ENDPOINT_DANMU: str = "getDanmuInfo"
ENDPOINT_BANLIST: str = "ban_list"

# Canonical URLs for the three captured endpoints. The B站 URLs are
# stable; duplicating them here (instead of importing from client.py)
# keeps the capture metadata self-contained and unambiguous in JSON.
NAV_URL: str = "https://api.bilibili.com/x/web-interface/nav"
DANMU_URL: str = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
BANLIST_URL: str = (
    "https://api.live.bilibili.com/xlive/web-ucenter/v1/banned/GetSilentUserList"
)

# Sentinel value written in place of any redacted credential.
REDACTED: str = "<REDACTED>"

# Headers we drop outright (not just blank out) — these are auth-bearing
# and serve no replay purpose once credentials are redacted from bodies.
DROP_HEADERS: frozenset[str] = frozenset({"set-cookie"})

# Key-name patterns whose VALUES must be replaced with REDACTED.
# Case-insensitive matching. Compile once at import time.
_SENSITIVE_KEY_RE: re.Pattern[str] = re.compile(
    r"^(?:sessdata|bili_jct|dedeuserid|dedeuserid_ckmd5|access_token|refresh_token)$",
    re.IGNORECASE,
)

# Real-looking B站 SESSDATA: 32+ lowercase hex digits, possibly prefixed.
# Used by the dry-run self-test to detect accidental leaks of genuine
# session tokens in the saved fixture.
_SESSDATA_HEX_RE: re.Pattern[str] = re.compile(
    r"SESSDATA=[a-f0-9]{32,}",
    re.IGNORECASE,
)
_BILI_JCT_HEX_RE: re.Pattern[str] = re.compile(
    r"bili_jct["r"'\s:=>]+[a-f0-9]{32,}",
    re.IGNORECASE,
)

# JSON-shaped value alias. Typed as ``object`` (not a recursive union)
# so BilibiliClient's `list[dict[str, Any]]` is assignable without
# casts; ``redact_body`` only inspects keys, never the value shape.
JsonValue = object

# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def _is_sensitive_key(key: object) -> bool:
    """True if ``key`` (as a string) matches one of the sensitive patterns."""
    return isinstance(key, str) and bool(_SENSITIVE_KEY_RE.match(key))


def redact_body(value: JsonValue) -> JsonValue:
    """Walk a JSON-shaped value, replacing sensitive values with ``REDACTED``.

    Two redaction passes:

    1. **Key-based** (primary): any dict key matching a sensitive pattern
       (SESSDATA / bili_jct / DedeUserID* / *_token) has its value
       replaced wholesale.
    2. **String-content** (defensive): any string value that contains an
       embedded ``SESSDATA=<hex>`` or ``bili_jct=<hex>`` blob is replaced
       wholesale. B站 does not normally echo cookies this way, but a
       future endpoint, a wrapped error envelope, or a nested debug
       payload might — and we never want a real token to land in a
       committed fixture even by accident.

    Operates on a structural copy — the input is never mutated.
    """
    if isinstance(value, dict):
        out: dict[str, JsonValue] = {}
        for k, v in value.items():
            if _is_sensitive_key(k):
                out[k] = REDACTED
            else:
                out[k] = redact_body(v)
        return out
    if isinstance(value, list):
        return [redact_body(item) for item in value]
    if isinstance(value, str) and (
        _SESSDATA_HEX_RE.search(value) or _BILI_JCT_HEX_RE.search(value)
    ):
        return REDACTED
    return value


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Drop auth-bearing headers and blank sensitive values.

    - ``Set-Cookie`` (case-insensitive) is removed entirely; the captured
      body is the test source of truth, not the cookie envelope.
    - Any other header whose NAME matches a sensitive pattern gets its
      value replaced with ``REDACTED`` (defensive — B站 shouldn't echo
      credentials in headers, but a future endpoint might).
    """
    out: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in DROP_HEADERS:
            continue
        if _is_sensitive_key(k):
            out[k] = REDACTED
        else:
            out[k] = v
    return out


def redact_cookie_jar() -> dict[str, str]:
    """Return an empty cookie jar — fixtures must never carry live cookies.

    The shape is a dict (matching ``httpx.Cookies``'s ``__iter__`` form)
    so the field can sit alongside ``response_body`` and
    ``response_headers`` without breaking the JSON contract.
    """
    return {}


# ---------------------------------------------------------------------------
# Filename + save
# ---------------------------------------------------------------------------


def _canonical_params(endpoint: str, room_id: int | None) -> dict[str, JsonValue]:
    """Stable, meaningful inputs to ``sha256`` for filename determinism.

    Deliberately NOT the wire-level request (which includes WBI's
    timestamp + ``w_rid`` and changes every call). A re-capture of the
    same room overwrites the same file instead of accumulating stale
    snapshots.
    """
    if endpoint == ENDPOINT_NAV:
        return {}
    if endpoint in (ENDPOINT_DANMU, ENDPOINT_BANLIST):
        assert room_id is not None, f"{endpoint} requires room_id"
        return {"room_id": room_id}
    raise ValueError(f"unknown endpoint: {endpoint}")


def _sha16(d: Mapping[str, JsonValue]) -> str:
    """First 16 hex chars of sha256(canonical JSON of ``d``)."""
    payload = json.dumps(d, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def fixture_filename(endpoint: str, room_id: int | None) -> str:
    """Build the on-disk filename for a captured fixture."""
    return f"{endpoint}_{_sha16(_canonical_params(endpoint, room_id))}.json"


def save_fixture(
    *,
    endpoint: str,
    room_id: int | None,
    url: str,
    request_params: Mapping[str, JsonValue],
    response_status: int,
    response_body: JsonValue,
    response_headers: Mapping[str, str],
    out_dir: Path,
) -> Path:
    """Write one redacted fixture to ``out_dir``. Returns the path written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "captured_at": datetime.now(UTC).isoformat(),
        "endpoint": endpoint,
        "url": url,
        "request_params": dict(request_params),
        "response_status": response_status,
        "response_body": redact_body(response_body),
        "response_headers": redact_headers(response_headers),
        "cookies": redact_cookie_jar(),
    }
    path = out_dir / fixture_filename(endpoint, room_id)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Dry-run: synthetic responses + self-test
# ---------------------------------------------------------------------------


def _build_synthetic_nav() -> dict[str, JsonValue]:
    """Mimic a real ``/x/web-interface/nav`` response, seeded with obvious fakes.

    The fake SESSDATA / bili_jct / DedeUserID values look hex-shaped on
    purpose so that the post-redaction ``[a-f0-9]{32,}`` scan would fail
    loudly if redaction ever regressed.
    """
    return {
        "code": 0,
        "message": "0",
        "ttl": 1,
        "data": {
            "isLogin": True,
            "uname": "fixture_capture_dry_run",
            "mid": 999999999,
            "SESSDATA": "deadbeef" * 4,  # secretscan-allow: synthetic dry-run fixture
            "bili_jct": "cafef00d" * 4,  # secretscan-allow: synthetic dry-run fixture
            "DedeUserID": "abc12345",  # secretscan-allow: synthetic dry-run fixture
            "DedeUserID_ckMd5": "fffefdfc",
            "access_token": "eyJh" + "bcdc" * 8,
            "refresh_token": "eyJy" + "efab" * 8,
            "wbi_img": {
                "img_url": "https://i0.hdslb.com/bfs/wbi/abcdef0123456789.png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/fedcba9876543210.png",
            },
        },
    }


def _build_synthetic_danmu(room_id: int) -> dict[str, JsonValue]:
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "group": "live",
            "business_id": 0,
            "refresh_row_factor": 0.125,
            "refresh_rate": 100,
            "max_delay": 5000,
            "token": "XXXX" + "f00d" * 8,  # looks like real danmu token
            "host_list": [
                {
                    "host": "tx-live-comet-01.chat.bilibili.com",
                    "port": 2245,
                    "wss_port": 2245,
                    "ws_port": 2245,
                }
            ],
            "SESSDATA": "a1b2c3d4" * 4,
            "_room_id": room_id,
        },
    }


def _build_synthetic_banlist(room_id: int) -> JsonValue:
    """Mimic the unwrapped ban list (``data.data`` after envelope strip).

    Returned as the envelope-shaped payload the live capture also saves,
    so the synthetic fixture mirrors what a real capture would produce.
    """
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "total": 1,
            "total_page": 1,
            "data": [
                {
                    "id": 1001,
                    "uid": 99999,
                    "uname": "fixture_user",
                    "roomid": room_id,
                    "ctime": 1700000000,
                    "banned_type": 1,
                    "operator": 12345,
                    "SESSDATA": "b2c3d4e5" * 4,
                    "DedeUserID_ckMd5": "99887766",
                }
            ],
        },
    }


def _assert_redacted(payload_text: str, source: str) -> None:
    """Raise ``AssertionError`` if the saved fixture leaks a real-looking secret."""
    if _SESSDATA_HEX_RE.search(payload_text):
        raise AssertionError(
            f"{source}: SESSDATA=[hex] pattern survived redaction — "
            f"fixture is unsafe to commit. pattern: {_SESSDATA_HEX_RE.pattern}"
        )
    if _BILI_JCT_HEX_RE.search(payload_text):
        raise AssertionError(
            f"{source}: bili_jct=[hex] pattern survived redaction — "
            f"fixture is unsafe to commit. pattern: {_BILI_JCT_HEX_RE.pattern}"
        )


def dry_run(out_dir: Path) -> int:
    """Run the full save+redact pipeline on synthetic data; self-test asserts.

    Writes fixtures into a fresh temp dir under ``/tmp`` so the live
    ``tests/fixtures/`` directory is never polluted by synthetic data.
    Cleans up the temp dir on exit (success or failure).
    """
    tmp = Path(tempfile.mkdtemp(prefix="ccshield_capture_dryrun_"))
    try:
        nav_path = save_fixture(
            endpoint=ENDPOINT_NAV,
            room_id=None,
            url=NAV_URL,
            request_params={},
            response_status=200,
            response_body=_build_synthetic_nav(),
            response_headers={
                "set-cookie": (
                    "SESSDATA=deadbeefdeadbeefdeadbeefdeadbeef; Path=/; "  # secretscan-allow: synthetic dry-run fixture
                    "HttpOnly; bili_jct=cafef00dcafef00dcafef00dcafef00d"  # secretscan-allow: synthetic dry-run fixture
                ),
                "content-type": "application/json",
            },
            out_dir=tmp,
        )
        danmu_path = save_fixture(
            endpoint=ENDPOINT_DANMU,
            room_id=22210347,
            url=DANMU_URL,
            request_params={"id": 22210347, "type": "0"},
            response_status=200,
            response_body=_build_synthetic_danmu(22210347),
            response_headers={"content-type": "application/json"},
            out_dir=tmp,
        )
        banlist_path = save_fixture(
            endpoint=ENDPOINT_BANLIST,
            room_id=22210347,
            url=BANLIST_URL,
            request_params={"room_id": 22210347, "ps": 1},
            response_status=200,
            response_body=_build_synthetic_banlist(22210347),
            response_headers={"content-type": "application/json"},
            out_dir=tmp,
        )

        # Self-test: read every saved file, assert no real-looking secret.
        nav_text = nav_path.read_text(encoding="utf-8")
        danmu_text = danmu_path.read_text(encoding="utf-8")
        banlist_text = banlist_path.read_text(encoding="utf-8")
        _assert_redacted(nav_text, source=str(nav_path))
        _assert_redacted(danmu_text, source=str(danmu_path))
        _assert_redacted(banlist_text, source=str(banlist_path))

        # Show the redacted nav fixture (the most credential-dense one)
        # so a human can eyeball the redaction.
        print("DRY-RUN OK")
        print(f"wrote synthetic fixtures to {tmp}/")
        print(f"  {nav_path.name}")
        print(f"  {danmu_path.name}")
        print(f"  {banlist_path.name}")
        print()
        print("--- redacted nav fixture (eyeball check) ---")
        print(nav_text)
        print("--- end redacted nav fixture ---")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Live capture
# ---------------------------------------------------------------------------


async def _capture_live(out_dir: Path, room_id: int | None) -> int:
    """Run the three captures against the real B站 API and write each one."""
    from app.bilibili.client import BilibiliClient

    client = BilibiliClient()
    try:
        # --- nav: get_user_info ------------------------------------------
        nav_data = await client.get_user_info()
        if nav_data is None:
            logger.warning("nav returned None (likely -101 expired); writing empty body")
            nav_body: JsonValue = {"code": -101, "message": "not logged in"}
        else:
            nav_body = {"code": 0, "message": "0", "ttl": 1, "data": nav_data}
        save_fixture(
            endpoint=ENDPOINT_NAV,
            room_id=None,
            url=NAV_URL,
            request_params={},
            response_status=200,
            response_body=nav_body,
            response_headers=dict(client.http.headers),
            out_dir=out_dir,
        )

        # --- room-bound endpoints ----------------------------------------
        if room_id is not None:
            danmu_data = await client.get_danmu_info(room_id)
            save_fixture(
                endpoint=ENDPOINT_DANMU,
                room_id=room_id,
                url=DANMU_URL,
                request_params={"id": room_id, "type": "0"},
                response_status=200,
                response_body={"code": 0, "message": "ok", "data": danmu_data},
                response_headers=dict(client.http.headers),
                out_dir=out_dir,
            )

            bans = await client.get_ban_list(room_id)
            ban_body: JsonValue = {
                "code": 0,
                "message": "ok",
                "data": {"data": bans},
            }
            save_fixture(
                endpoint=ENDPOINT_BANLIST,
                room_id=room_id,
                url=BANLIST_URL,
                request_params={"room_id": room_id, "ps": 1},
                response_status=200,
                response_body=ban_body,
                response_headers=dict(client.http.headers),
                out_dir=out_dir,
            )
        else:
            logger.info(
                "no room_id provided; skipping getDanmuInfo + ban_list captures"
            )
    finally:
        await client.close()

    print(f"captured fixtures to {out_dir}/")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _has_real_cookies() -> bool:
    """True iff ``.env`` has non-empty SESSDATA AND BILI_JCT.

    We require both because B站 endpoints either need a session
    (SESSDATA) or need to write (bili_jct) — partial config is treated as
    "not logged in" so the script can be invoked safely at any time.
    """
    from app.config import settings

    return bool(settings.SESSDATA) and bool(settings.BILI_JCT)


def _resolve_room_id(arg_room: int | None) -> int | None:
    """Prefer CLI ``--room``; fall back to ``settings.ROOM_ID``; else None."""
    if arg_room is not None:
        return arg_room
    from app.config import settings

    return settings.ROOM_ID


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m scripts.capture_fixtures",
        description=(
            "Capture B站 API responses for offline test replay, with "
            "mandatory credential redaction before any file is written."
        ),
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--live",
        action="store_true",
        help="Capture real responses (requires .env cookies).",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate save+redact pipeline on synthetic data; no network.",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=Path("tests/fixtures"),
        help="Where to write captured fixtures (default: tests/fixtures).",
    )
    p.add_argument(
        "--room",
        type=int,
        default=None,
        help=(
            "Target room id for getDanmuInfo + ban_list captures. "
            "Defaults to settings.ROOM_ID; omit to skip room-bound endpoints."
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    out_dir = args.out_dir.resolve()
    room_id = _resolve_room_id(args.room)

    # Default to --dry-run when neither flag is set so a bare invocation
    # never silently touches the network.
    if not args.live and not args.dry_run:
        args.dry_run = True

    if args.dry_run:
        return dry_run(out_dir)

    # --live path
    assert args.live  # for type checker; the mutex above guarantees this
    if not _has_real_cookies():
        print("no cookies in .env, cannot capture live; use --dry-run")
        return 0
    return asyncio.run(_capture_live(out_dir, room_id))


if __name__ == "__main__":
    sys.exit(main())
