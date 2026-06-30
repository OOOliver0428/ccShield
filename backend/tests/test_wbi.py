"""TDD: tests for WBI signing (wbi.py).

These tests codify the WBI sign algorithm port:
- MIXIN_KEY_ENC_TAB (64-int table)
- get_mixin_key(orig)
- enc_wbi(params, img_key, sub_key)
- WbiSigner caches keys 1h, fetches from /x/web-interface/nav

TDD step 1: write tests FIRST. They MUST fail before implementation exists.

Oracle vectors come from the canonical bilibili-API-collect documented pair
(img_key=7cd0849..., sub_key=4932caff...) and pre-computed independently by
running ccShield's wbi.py logic with a pinned wts value, so the test is
deterministic and reference-independent.
"""
from __future__ import annotations

import hashlib
from typing import Any

import httpx
import pytest

from app.bilibili.wbi import (
    MIXIN_KEY_ENC_TAB,
    WbiSigner,
    enc_wbi,
    get_mixin_key,
    wbi_signer,
)

# ---------------------------------------------------------------------------
# Algorithm constants and pure functions
# ---------------------------------------------------------------------------


def test_mixin_key_enc_tab_has_64_unique_indices() -> None:
    """The table must contain each index 0..63 exactly once."""
    assert len(MIXIN_KEY_ENC_TAB) == 64
    assert sorted(MIXIN_KEY_ENC_TAB) == list(range(64))


def test_get_mixin_key_known_pair() -> None:
    """Oracle: img+sub concatenation reordered by MIXIN_KEY_ENC_TAB, first 32 chars.

    Reference pair taken from bilibili-API-collect:
      img_key = 7cd084941338484aae1ad9425b84077c
      sub_key = 4932caff0ff746eab6f01bf08b70ac45
      expected mixin_key = ea1db124af3c7062474693fa704f4ff8
    """
    img = "7cd084941338484aae1ad9425b84077c"
    sub = "4932caff0ff746eab6f01bf08b70ac45"
    assert get_mixin_key(img + sub) == "ea1db124af3c7062474693fa704f4ff8"


# ---------------------------------------------------------------------------
# enc_wbi() oracle vectors (pre-computed w_rid values)
# ---------------------------------------------------------------------------


IMG_KEY = "7cd084941338484aae1ad9425b84077c"
SUB_KEY = "4932caff0ff746eab6f01bf08b70ac45"
PINNED_WTS = "1700000000"  # 2023-11-14T22:13:20Z

# Oracle w_rid for {'foo': '114'} with wts=1700000000
# Independently computed via ccShield's wbi.enc_wbi().
EXPECTED_W_RID_FOO_114 = "a2c93614b551780bddf8b06fd78730e1"

# Oracle for filtered special chars: 'a!b\'c(d)e*f' -> 'abcdef'
EXPECTED_W_RID_FILTERED = "9c6a4a87bac4f732ce047e9e29cee459"


def test_enc_wbi_returns_signed_params_including_wts_and_w_rid() -> None:
    # Inject deterministic wts via monkeypatch of time.time inside wbi module.
    import app.bilibili.wbi as wbi_mod

    original_time = wbi_mod.time.time
    wbi_mod.time.time = lambda: 1700000000.0
    try:
        signed = enc_wbi({"foo": "114"}, IMG_KEY, SUB_KEY)
    finally:
        wbi_mod.time.time = original_time

    assert signed["foo"] == "114"
    assert signed["wts"] == PINNED_WTS
    assert signed["w_rid"] == EXPECTED_W_RID_FOO_114


def test_enc_wbi_filters_special_chars_per_spec() -> None:
    """!'()* characters in any value must be stripped per B站 spec."""
    import app.bilibili.wbi as wbi_mod

    original_time = wbi_mod.time.time
    wbi_mod.time.time = lambda: 1700000000.0
    try:
        signed = enc_wbi({"key": "a!b'c(d)e*f"}, IMG_KEY, SUB_KEY)
    finally:
        wbi_mod.time.time = original_time

    assert signed["key"] == "abcdef"
    assert signed["w_rid"] == EXPECTED_W_RID_FILTERED


def test_enc_wbi_sorts_keys_alphabetically() -> None:
    signed = enc_wbi({"b": "2", "a": "1", "c": "3"}, IMG_KEY, SUB_KEY)
    keys = list(signed.keys())
    # The "w_rid" is appended after sorting per the algorithm — verify sorted
    # order of original keys precedes w_rid.
    assert keys[:3] == ["a", "b", "c"]
    assert keys[-1] == "w_rid"


def test_enc_wbi_does_not_mutate_input() -> None:
    original: dict[str, str] = {"foo": "114"}
    enc_wbi(original, IMG_KEY, SUB_KEY)
    assert original == {"foo": "114"}


def test_enc_wbi_oracle_against_standalone_md5() -> None:
    """Sanity: replicate the algorithm in the test and assert our impl agrees.

    This guards against silent divergence from ccShield by recomputing
    w_rid locally and asserting equality.
    """
    import app.bilibili.wbi as wbi_mod

    params = {"foo": "114"}

    # Standalone replication
    mixin_key = get_mixin_key(IMG_KEY + SUB_KEY)
    expected_query = "foo=114&wts=" + PINNED_WTS
    standalone_rid = hashlib.md5(
        (expected_query + mixin_key).encode()
    ).hexdigest()
    assert standalone_rid == EXPECTED_W_RID_FOO_114

    # And our port produces the same
    original_time = wbi_mod.time.time
    wbi_mod.time.time = lambda: 1700000000.0
    try:
        signed = enc_wbi(dict(params), IMG_KEY, SUB_KEY)
    finally:
        wbi_mod.time.time = original_time
    assert signed["w_rid"] == standalone_rid


# ---------------------------------------------------------------------------
# WbiSigner: key caching + nav fetch
# ---------------------------------------------------------------------------


def _make_handler(payload: dict[str, Any], status_code: int = 200) -> httpx.MockTransport:
    """Build an httpx MockTransport that returns a fixed JSON payload."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return httpx.MockTransport(handler)


def test_module_level_wbi_signer_is_WbiSigner_instance() -> None:
    assert isinstance(wbi_signer, WbiSigner)


def test_wbi_signer_fetches_keys_from_nav_on_first_call() -> None:
    signer = WbiSigner()
    nav_payload = {
        "code": 0,
        "data": {
            "wbi_img": {
                "img_url": (
                    "https://i0.hdslb.com/bfs/wbi/"
                    + IMG_KEY
                    + ".png"
                ),
                "sub_url": (
                    "https://i0.hdslb.com/bfs/wbi/"
                    + SUB_KEY
                    + ".png"
                ),
            }
        },
    }
    transport = _make_handler(nav_payload)
    import asyncio

    inner = httpx.AsyncClient(transport=transport)
    try:
        keys = asyncio.run(signer.get_keys(inner))
    finally:
        asyncio.run(inner.aclose())
    assert keys == (IMG_KEY, SUB_KEY)
    assert signer.last_update > 0


def test_wbi_signer_caches_keys_within_refresh_interval() -> None:
    """A second get_keys call within 1h must NOT hit the network again."""
    import asyncio

    signer = WbiSigner()
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "wbi_img": {
                        "img_url": "https://i0.hdslb.com/bfs/wbi/" + IMG_KEY + ".png",
                        "sub_url": "https://i0.hdslb.com/bfs/wbi/" + SUB_KEY + ".png",
                    }
                },
            },
        )

    transport = httpx.MockTransport(handler)
    inner = httpx.AsyncClient(transport=transport)
    try:
        k1 = asyncio.run(signer.get_keys(inner))
        import app.bilibili.wbi as wbi_mod

        signer.last_update = wbi_mod.time.time()
        k2 = asyncio.run(signer.get_keys(inner))
    finally:
        asyncio.run(inner.aclose())

    assert k1 == k2
    assert call_count == 1


def test_wbi_signer_refreshes_after_interval() -> None:
    """If last_update is older than refresh_interval, network is hit again."""
    signer = WbiSigner()
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "wbi_img": {
                        "img_url": "https://i0.hdslb.com/bfs/wbi/" + IMG_KEY + ".png",
                        "sub_url": "https://i0.hdslb.com/bfs/wbi/" + SUB_KEY + ".png",
                    }
                },
            },
        )

    import asyncio

    transport = httpx.MockTransport(handler)
    inner = httpx.AsyncClient(transport=transport)
    try:
        asyncio.run(signer.get_keys(inner))
        signer.last_update = 0.0
        asyncio.run(signer.get_keys(inner))
    finally:
        asyncio.run(inner.aclose())

    assert call_count == 2


def test_wbi_signer_sign_returns_signed_params() -> None:
    """sign() end-to-end: cached keys + enc_wbi → w_rid present."""
    import asyncio

    signer = WbiSigner()
    nav_payload = {
        "code": 0,
        "data": {
            "wbi_img": {
                "img_url": "https://i0.hdslb.com/bfs/wbi/" + IMG_KEY + ".png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/" + SUB_KEY + ".png",
            }
        },
    }
    transport = _make_handler(nav_payload)
    inner = httpx.AsyncClient(transport=transport)
    try:
        signed = asyncio.run(signer.sign(inner, {"id": "22210347", "type": "0"}))
    finally:
        asyncio.run(inner.aclose())
    assert "w_rid" in signed
    assert "wts" in signed
    assert signed["id"] == "22210347"
    assert signed["type"] == "0"


def test_wbi_signer_raises_when_keys_unavailable() -> None:
    """If nav returns non-zero AND no cached keys, get_keys must raise."""
    import asyncio

    signer = WbiSigner()
    transport = _make_handler({"code": -101, "message": "no auth"})
    inner = httpx.AsyncClient(transport=transport)
    try:
        with pytest.raises(Exception):
            asyncio.run(signer.get_keys(inner))
    finally:
        asyncio.run(inner.aclose())


def test_wbi_signer_raises_on_unexpected_payload_shape() -> None:
    """Missing wbi_img key must surface an exception (not silently return None)."""
    import asyncio

    signer = WbiSigner()
    transport = _make_handler({"code": 0, "data": {}})
    inner = httpx.AsyncClient(transport=transport)
    try:
        with pytest.raises(Exception):
            asyncio.run(signer.get_keys(inner))
    finally:
        asyncio.run(inner.aclose())
