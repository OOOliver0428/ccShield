"""TDD: tests for typed Bilibili HTTP client (app.bilibili.client).

These tests describe the contract that BilibiliClient must fulfill:
- typed return values where possible
- typed exceptions on non-zero business codes (-101/-403/-509/-other)
- WBI is only used for get_danmu_info (and that one method retries once on -352)
- get_ban_list paginates all pages
- resolve_room_id handles real-id-first, short-id-fallback
- resolve_room_id does NOT make the redundant _get_anchor_name call that
  ccShield's bili_client.py made (removed per spec)

TDD step 1: write tests FIRST. They MUST fail before implementation exists.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from app.bilibili.client import BilibiliClient
from app.bilibili.exceptions import (
    AuthExpiredError,
    BiliApiError,
    PermissionDeniedError,
    RateLimitedError,
)
from app.bilibili.wbi import WbiSigner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_client(handler) -> BilibiliClient:
    """Build a BilibiliClient with an httpx.MockTransport (sync).

    Always uses a fresh `WbiSigner` so per-test WBI cache state does not
    leak between tests (the module-level `wbi_signer` is shared across the
    process and would otherwise leak keys between test runs).
    """
    transport = httpx.MockTransport(handler)
    inner = httpx.AsyncClient(transport=transport)
    return BilibiliClient(
        client=inner, cookies={}, csrf_token="", signer=WbiSigner()
    )


def run(coro):
    """Run an async coroutine to completion from a sync test."""
    import asyncio

    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# get_user_info (nav)
# ---------------------------------------------------------------------------


def test_get_user_info_returns_data_on_code_0() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "uname": "tester",
                    "mid": 12345,
                    "wbi_img": {"img_url": "x", "sub_url": "y"},
                },
            },
        )

    client = make_client(handler)
    result = run(client.get_user_info())
    assert result is not None
    assert result["uname"] == "tester"
    assert result["mid"] == 12345
    assert captured[0].url.path == "/x/web-interface/nav"


def test_get_user_info_returns_none_on_minus_101_for_auth_check() -> None:
    """nav is special: callers (auth flow) need to detect expired cookies without
    raising — keep the documented exception that get_user_info may return None on
    non-zero codes (so the auth flow can detect expired cookies).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -101, "message": "expired"})

    client = make_client(handler)
    assert run(client.get_user_info()) is None


def test_get_active_super_chats_extracts_room_bootstrap_list() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/x/web-interface/nav":
            return httpx.Response(
                200,
                json={
                    "code": -101,
                    "data": {
                        "wbi_img": {
                            "img_url": (
                                "https://i.example/"
                                "0123456789abcdef0123456789abcdef.png"
                            ),
                            "sub_url": (
                                "https://i.example/"
                                "fedcba9876543210fedcba9876543210.png"
                            ),
                        }
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "super_chat_info": {
                        "message_list": [
                            {"id": 1, "message": "active"},
                            "malformed",
                        ]
                    }
                },
            },
        )

    client = make_client(handler)
    result = run(client.get_active_super_chats(24696014))
    assert result == [{"id": 1, "message": "active"}]
    request = captured[-1]
    assert request.url.path.endswith("/getInfoByRoom")
    assert request.url.params["room_id"] == "24696014"
    assert "w_rid" in request.url.params
    assert "wts" in request.url.params


def test_close_closes_injected_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -101, "message": "x"})

    client = make_client(handler)
    run(client.close())
    # Re-using closed client must raise; if aclose didn't close, the
    # subsequent get_user_info would not raise.
    with pytest.raises(RuntimeError):
        run(client.get_user_info())


# ---------------------------------------------------------------------------
# update_cookies — refresh the underlying httpx cookie jar in place
# ---------------------------------------------------------------------------
#
# Bug fix: the long-lived ``BilibiliClient`` singleton is constructed at
# module-import time when ``.env`` is empty, so its httpx cookie jar is
# empty. After a successful QR / manual login mutates ``settings`` in
# place, the existing client's jar must be refreshed without rebuilding
# the whole client (which would discard the keep-alive connection pool).
# ``update_cookies`` pushes the new cookies into both the underlying
# ``httpx.AsyncClient.cookies`` jar AND the in-memory ``_cookies`` mirror.


def test_update_cookies_sets_sessdata_in_underlying_httpx_jar() -> None:
    """After update_cookies, the httpx client's jar exposes the new SESSDATA."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -101, "message": "x"})

    client = make_client(handler)
    # Pre-condition: no SESSDATA in the jar.
    assert client.http.cookies.get("SESSDATA") is None

    client.update_cookies({"SESSDATA": "new", "bili_jct": "jct-new"})

    assert client.http.cookies.get("SESSDATA") == "new"
    assert client.http.cookies.get("bili_jct") == "jct-new"
    assert client.csrf_token == "jct-new"


def test_update_cookies_overwrites_existing_values() -> None:
    """update_cookies REPLACES entries that share a key (per-cookie upsert)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -101, "message": "x"})

    client = make_client(handler)
    client.update_cookies({"SESSDATA": "first"})
    assert client.http.cookies.get("SESSDATA") == "first"

    client.update_cookies({"SESSDATA": "second"})
    assert client.http.cookies.get("SESSDATA") == "second"


def test_update_cookies_updates_internal_mirror() -> None:
    """The internal ``_cookies`` mirror must also reflect the new values."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -101, "message": "x"})

    client = make_client(handler)
    client.update_cookies({"SESSDATA": "mirror-check"})
    assert client._cookies.get("SESSDATA") == "mirror-check"


def test_update_cookies_preserves_unrelated_keys() -> None:
    """Updating one cookie must not erase a different cookie that was set earlier."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -101, "message": "x"})

    client = make_client(handler)
    client.update_cookies({"SESSDATA": "a", "bili_jct": "j", "buvid3": "b"})
    # Re-update with only SESSDATA — bili_jct and buvid3 must survive.
    client.update_cookies({"SESSDATA": "a2"})
    assert client.http.cookies.get("bili_jct") == "j"
    assert client.http.cookies.get("buvid3") == "b"
    assert client.http.cookies.get("SESSDATA") == "a2"
    assert client.get_cookie("buvid3") == "b"


def test_update_cookies_with_empty_dict_is_noop() -> None:
    """Empty dict must not raise; it just keeps the current state."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -101, "message": "x"})

    client = make_client(handler)
    client.update_cookies({"SESSDATA": "kept"})
    client.update_cookies({})
    assert client.http.cookies.get("SESSDATA") == "kept"


# ---------------------------------------------------------------------------
# get_room_init / get_room_info / resolve_room_id
# ---------------------------------------------------------------------------


def test_get_room_init_returns_data_on_code_0() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "room_id": 22210347,
                    "short_id": 0,
                    "uid": 111111,
                },
            },
        )

    client = make_client(handler)
    data = run(client.get_room_init(22210347))
    assert data is not None
    assert data["room_id"] == 22210347


def test_get_room_info_returns_data_without_extra_anchor_name_fetch() -> None:
    """get_room_info MUST return data as-is, without the redundant _get_anchor_name
    double-fetch that ccShield did. We assert only one HTTP call.
    """
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "room_id": 22210347,
                    "title": "stream title",
                    "uid": 111111,
                    "live_status": 1,
                },
            },
        )

    client = make_client(handler)
    data = run(client.get_room_info(22210347))
    assert data is not None
    assert data["room_id"] == 22210347
    assert call_count["n"] == 1  # no double-fetch


def test_get_anchor_info_returns_public_uname() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/live_user/v1/Master/info")
        assert request.url.params.get("uid") == "999"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"info": {"uid": 999, "uname": "anchor-name"}},
            },
        )

    client = make_client(handler)
    info = run(client.get_anchor_info(999))

    assert info == {"uid": 999, "uname": "anchor-name"}


def test_get_room_user_role_prefers_anchor_without_fetching_admins() -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/x/web-interface/nav":
            return httpx.Response(200, json={"code": 0, "data": {"mid": 42}})
        if request.url.path == "/room/v1/Room/room_init":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"room_id": 1601605, "uid": 42}},
            )
        raise AssertionError(f"unexpected request: {request.url}")

    client = make_client(handler)
    assert run(client.get_room_user_role(1601605)) == "anchor"
    assert not any("roomAdmin" in path for path in requested_paths)


def test_get_room_user_role_finds_admin_across_pages() -> None:
    requested_pages: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/nav":
            return httpx.Response(200, json={"code": 0, "data": {"mid": 55}})
        if request.url.path == "/room/v1/Room/room_init":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"room_id": 1601605, "uid": 99}},
            )
        if request.url.path.endswith("/roomAdmin/get_by_room"):
            page = request.url.params.get("page")
            requested_pages.append(page)
            rows = [{"uid": 7}] if page == "1" else [{"uid": "55"}]
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "page": {"page": int(page or "1"), "total_page": 2},
                        "data": rows,
                    },
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    client = make_client(handler)
    assert run(client.get_room_user_role(1601605)) == "admin"
    assert requested_pages == ["1", "2"]


def test_get_room_user_role_returns_viewer_after_complete_admin_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/nav":
            return httpx.Response(200, json={"code": 0, "data": {"mid": 55}})
        if request.url.path == "/room/v1/Room/room_init":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"room_id": 1601605, "uid": 99}},
            )
        if request.url.path.endswith("/roomAdmin/get_by_room"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "page": {"page": 1, "total_page": 1},
                        "data": [{"uid": 7}],
                    },
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    client = make_client(handler)
    assert run(client.get_room_user_role(1601605)) == "viewer"


def test_get_room_user_role_does_not_mislabel_failed_admin_lookup() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/nav":
            return httpx.Response(200, json={"code": 0, "data": {"mid": 55}})
        if request.url.path == "/room/v1/Room/room_init":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"room_id": 1601605, "uid": 99}},
            )
        if request.url.path.endswith("/roomAdmin/get_by_room"):
            return httpx.Response(200, json={"code": -400, "message": "bad"})
        raise AssertionError(f"unexpected request: {request.url}")

    client = make_client(handler)
    assert run(client.get_room_user_role(1601605)) == "unknown"


def test_resolve_room_id_enriches_missing_anchor_name() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path.endswith("/room/v1/Room/get_info"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "room_id": 1601605,
                        "title": "Live title",
                        "uid": 999,
                        "live_status": 1,
                    },
                },
            )
        if request.url.path.endswith("/room/v1/Room/room_init"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "room_id": 1601605,
                        "short_id": 0,
                        "uid": 999,
                    },
                },
            )
        if request.url.path.endswith("/live_user/v1/Master/info"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"info": {"uid": 999, "uname": "主播名"}},
                },
            )
        return httpx.Response(404)

    client = make_client(handler)
    info = run(client.resolve_room_id(1601605))

    assert info is not None
    assert info["room_id"] == 1601605
    assert info["title"] == "Live title"
    assert info["uname"] == "主播名"
    assert seen_paths == [
        "/room/v1/Room/get_info",
        "/room/v1/Room/room_init",
        "/live_user/v1/Master/info",
    ]


def test_resolve_room_id_with_real_id() -> None:
    """If get_room_info returns room_id, the input is a real id → is_short_id=False.

    Call order:
      - get_room_info(22210347) → has room_id
      - get_room_init(22210347) → fills uid/short_id
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/room/v1/Room/get_info"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "room_id": 22210347,
                        "title": "t",
                        "live_status": 1,
                        "uname": "tester",
                    },
                },
            )
        if request.url.path.endswith("/room/v1/Room/room_init"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "room_id": 22210347,
                        "short_id": 0,
                        "uid": 111111,
                    },
                },
            )
        return httpx.Response(404)

    client = make_client(handler)
    info = run(client.resolve_room_id(22210347))
    assert info is not None
    assert info["room_id"] == 22210347
    assert info["is_short_id"] is False
    assert info["uid"] == 111111
    assert info["short_id"] == 0


def test_resolve_room_id_with_short_id() -> None:
    """If get_room_info fails AND get_room_init translates a short id → is_short_id=True.

    Call order:
      - get_room_info(12345) → code != 0 (no such room)
      - get_room_init(12345) → room_id=22210347, short_id=12345
      - get_room_info(22210347) → enriches title/uname
    """
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/room/v1/Room/get_info"):
            tid = request.url.params.get("room_id", "")
            if tid == "12345":
                # Treat as "no such room" — B站 returns code != 0 here.
                return httpx.Response(
                    200,
                    json={"code": 1, "message": "not found", "data": None},
                )
            # Second call: real room
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "room_id": 22210347,
                        "title": "real title",
                        "live_status": 1,
                        "uname": "real-name",
                    },
                },
            )
        if path.endswith("/room/v1/Room/room_init"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "room_id": 22210347,
                        "short_id": 12345,
                        "uid": 999,
                    },
                },
            )
        return httpx.Response(404)

    client = make_client(handler)
    info = run(client.resolve_room_id(12345))
    assert info is not None
    assert info["room_id"] == 22210347
    assert info["short_id"] == 12345
    assert info["is_short_id"] is True
    assert info["uid"] == 999
    assert info["title"] == "real title"


def test_resolve_room_id_returns_none_when_both_fail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"code": 1, "message": "not found", "data": None}
        )

    client = make_client(handler)
    assert run(client.resolve_room_id(99999)) is None


# ---------------------------------------------------------------------------
# Error mapping on write APIs
# ---------------------------------------------------------------------------


def _post_handler(payload: dict[str, Any]) -> Any:
    def h(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return h


def test_ban_user_success_returns_true() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"code": 0, "data": {"id": 1}})

    client = make_client(handler)
    assert run(client.ban_user(room_id=22210347, uid=12345, hour=2, msg="stop")) is True

    assert captured[0].url.path.endswith(
        "/xlive/web-ucenter/v1/banned/AddSilentUser"
    )
    body = parse_qs(captured[0].content.decode(), keep_blank_values=True)
    assert body == {
        "room_id": ["22210347"],
        "tuid": ["12345"],
        "mobile_app": ["web"],
        "type": ["1"],
        "hour": ["2"],
        "csrf_token": [""],
        "csrf": [""],
    }


def test_ban_user_session_uses_official_type_two_contract() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"code": 0, "data": {"id": 1}})

    client = make_client(handler)
    assert run(client.ban_user(room_id=1601605, uid=12345, hour=0)) is True

    assert captured[0].url.path.endswith(
        "/xlive/web-ucenter/v1/banned/AddSilentUser"
    )
    body = parse_qs(captured[0].content.decode(), keep_blank_values=True)
    assert body == {
        "room_id": ["1601605"],
        "tuid": ["12345"],
        "mobile_app": ["web"],
        "type": ["2"],
        "hour": ["0"],
        "csrf_token": [""],
        "csrf": [""],
    }


@pytest.mark.parametrize("hour", [-1, 2, 4, 24, 168])
def test_ban_user_timed_and_permanent_use_type_one(hour: int) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"code": 0, "data": {"id": 1}})

    client = make_client(handler)
    assert run(client.ban_user(room_id=1601605, uid=12345, hour=hour)) is True

    assert captured[0].url.path.endswith(
        "/xlive/web-ucenter/v1/banned/AddSilentUser"
    )
    body = parse_qs(captured[0].content.decode(), keep_blank_values=True)
    assert body == {
        "room_id": ["1601605"],
        "tuid": ["12345"],
        "mobile_app": ["web"],
        "type": ["1"],
        "hour": [str(hour)],
        "csrf_token": [""],
        "csrf": [""],
    }


@pytest.mark.parametrize(
    ("code", "expected_exc"),
    [
        (-101, AuthExpiredError),
        (-403, PermissionDeniedError),
        (-509, RateLimitedError),
        (-500, BiliApiError),
    ],
)
def test_ban_user_maps_business_error_codes(code: int, expected_exc: type) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": code, "message": f"err {code}"})

    client = make_client(handler)
    with pytest.raises(expected_exc) as excinfo:
        run(client.ban_user(room_id=22210347, uid=1, hour=2))
    assert excinfo.value.code == code


def test_unban_user_success_returns_true() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": None})

    client = make_client(handler)
    assert run(client.unban_user(room_id=22210347, block_id=42)) is True


def test_unban_user_minus_101_raises_auth_expired() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -101, "message": "expired"})

    client = make_client(handler)
    with pytest.raises(AuthExpiredError):
        run(client.unban_user(room_id=22210347, block_id=42))


# ---------------------------------------------------------------------------
# get_danmu_info: WBI signed, with -352 refresh+retry
# ---------------------------------------------------------------------------


IMG_KEY = "7cd084941338484aae1ad9425b84077c"
SUB_KEY = "4932caff0ff746eab6f01bf08b70ac45"


def _auth_expired() -> dict[str, Any]:
    """Build a wbi-img nav response we can swap into the mock handler."""
    return {
        "code": 0,
        "data": {
            "wbi_img": {
                "img_url": "https://i0.hdslb.com/bfs/wbi/" + IMG_KEY + ".png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/" + SUB_KEY + ".png",
            }
        },
    }


def test_get_danmu_info_sends_wbi_signed_params() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/x/web-interface/nav"):
            return httpx.Response(200, json=_auth_expired())
        if request.url.path.endswith("/xlive/web-room/v1/index/getDanmuInfo"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"token": "X", "host_list": []},
                },
            )
        return httpx.Response(404)

    client = make_client(handler)
    result = run(client.get_danmu_info(22210347))
    assert result is not None
    danmu_calls = [r for r in captured if r.url.path.endswith("/getDanmuInfo")]
    assert len(danmu_calls) == 1
    qp = danmu_calls[0].url.params
    assert "w_rid" in qp
    assert "wts" in qp
    assert qp["id"] == "22210347"
    assert qp["type"] == "0"


def test_get_danmu_info_refreshes_wbi_on_minus_352_and_retries_once() -> None:
    """-352 means WBI stale; spec says refresh WBI + retry exactly once."""
    captured: list[httpx.Request] = []
    wbi_fetches = {"nav": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/x/web-interface/nav"):
            wbi_fetches["nav"] += 1
            return httpx.Response(200, json=_auth_expired())
        if request.url.path.endswith("/xlive/web-room/v1/index/getDanmuInfo"):
            if len([r for r in captured if r.url.path.endswith("/getDanmuInfo")]) == 1:
                return httpx.Response(200, json={"code": -352, "message": "wbi stale"})
            return httpx.Response(
                200, json={"code": 0, "data": {"token": "X", "host_list": []}}
            )
        return httpx.Response(404)

    client = make_client(handler)
    result = run(client.get_danmu_info(22210347))
    assert result is not None
    assert result["token"] == "X"
    # 2 danmu requests (fail + retry) + 2 nav requests (initial + forced refresh)
    danmu_calls = [r for r in captured if r.url.path.endswith("/getDanmuInfo")]
    assert len(danmu_calls) == 2
    assert wbi_fetches["nav"] == 2


def test_get_danmu_info_raises_after_minus_352_retry_exhausted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/x/web-interface/nav"):
            return httpx.Response(200, json=_auth_expired())
        if request.url.path.endswith("/xlive/web-room/v1/index/getDanmuInfo"):
            return httpx.Response(200, json={"code": -352, "message": "still stale"})
        return httpx.Response(404)

    client = make_client(handler)
    with pytest.raises(BiliApiError):
        run(client.get_danmu_info(22210347))


def handler_for_minus_101(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/x/web-interface/nav"):
        return httpx.Response(200, json=_auth_expired())
    if request.url.path.endswith("/xlive/web-room/v1/index/getDanmuInfo"):
        return httpx.Response(200, json={"code": -101, "message": "expired"})
    return httpx.Response(404)


def test_get_danmu_info_minus_101_raises_auth_expired_without_wbi_retry() -> None:
    """Non-352 codes must propagate immediately; no retry."""
    client = make_client(handler_for_minus_101)
    with pytest.raises(AuthExpiredError):
        run(client.get_danmu_info(22210347))


# ---------------------------------------------------------------------------
# get_ban_list: pagination
# ---------------------------------------------------------------------------


def _ban_list_payload(
    page: int, per_page: int, total_page: int, total: int
) -> dict[str, Any]:
    """Build a response body for /xlive/web-ucenter/v1/banned/GetSilentUserList."""
    items = [
        {
            "id": i,
            "uid": 1000 + i,
            "uname": f"user{i}",
            "room_id": 22210347,
        }
        for i in range((page - 1) * per_page, page * per_page)
    ]
    return {
        "code": 0,
        "data": {"data": items, "total": total, "total_page": total_page},
    }


def test_get_ban_list_paginates_and_merges_all_pages() -> None:
    seen_pages: list[int] = []
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/GetSilentUserList"):
            calls.append(request)
            ps = int(request.content.decode().split("ps=")[1].split("&")[0])
            seen_pages.append(ps)
            return httpx.Response(200, json=_ban_list_payload(ps, 2, 3, 6))
        return httpx.Response(404)

    client = make_client(handler)
    bans = run(client.get_ban_list(22210347))
    assert seen_pages == [1, 2, 3]
    assert len(bans) == 6
    # Verify ids across pages
    ids = sorted([b["id"] for b in bans])
    assert ids == [0, 1, 2, 3, 4, 5]


def test_get_ban_list_empty_returns_empty_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"data": [], "total": 0, "total_page": 1},
            },
        )

    client = make_client(handler)
    assert run(client.get_ban_list(22210347)) == []


def test_get_ban_list_sends_csrf_in_body() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"code": 0, "data": {"data": [], "total": 0, "total_page": 1}},
        )

    client = make_client(handler)
    run(client.get_ban_list(22210347))
    assert "csrf" in captured[0].content.decode()


def test_get_ban_list_minus_101_raises_auth_expired() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -101, "message": "expired"})

    client = make_client(handler)
    with pytest.raises(AuthExpiredError):
        run(client.get_ban_list(22210347))


def test_get_ban_list_caps_at_max_pages() -> None:
    """Safeguard: even if server reports total_page=999, cap at 100."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Always return a non-empty page so loop continues.
        return httpx.Response(200, json=_ban_list_payload(1, 1, 999, 999))

    client = make_client(handler)
    bans = run(client.get_ban_list(22210347))
    # At most 100 page requests' worth of items.
    assert len(bans) == 100


def test_get_ban_list_does_not_truncate_a_21_page_room() -> None:
    """Regression: a real 201-entry room spans 21 server-sized pages."""
    seen_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.content.decode().split("ps=")[1].split("&")[0])
        seen_pages.append(page)
        per_page = 1 if page == 21 else 10
        return httpx.Response(
            200,
            json=_ban_list_payload(page, per_page, 21, 201),
        )

    client = make_client(handler)
    bans = run(client.get_ban_list(1601605))

    assert seen_pages == list(range(1, 22))
    assert len(bans) == 201
