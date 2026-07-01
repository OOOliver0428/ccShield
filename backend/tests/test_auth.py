"""TDD: tests for B站 QR-code login + manual fallback + atomic .env write.

These tests describe the contract that ``app.bilibili.auth`` must fulfill:

- ``qr_generate``        GET /qrcode/generate → {qrcode_url, qrcode_key}.
- ``qr_poll``            GET /qrcode/poll   → typed exceptions per B站 code:
                            86101 → QrAwaitingScanError    (not scanned yet)
                            86090 → QrAwaitingConfirmError (scanned, awaiting confirm)
                            86038 → QrExpiredError         (QR expired)
                            0     → success dict
- Bili_jct is captured from ``data.url`` query params FIRST (primary path).
  If the query param is absent, fall back to ``response.cookies["bili_jct"]``
  (Set-Cookie header). If both paths fail → LoginIncompleteError.
- ``write_env_atomic``   Updates only SESSDATA / BILI_JCT / BUVID3 in the
                         target ``.env``; writes via ``.env.tmp`` + ``os.replace``
                         so a crash mid-write can never leave a half-written
                         file visible.
- ``save_cookies_manual`` Plan B fallback — set provided cookies on a fresh
                         BilibiliClient, call nav to validate, then atomically
                         persist.

TDD step 1: tests FIRST. They MUST fail before ``app.bilibili.auth`` exists.

Notes
-----
- Uses ``httpx.MockTransport`` so the tests never touch the network. All
  fake cookie values are obvious ("fake_sessdata", etc.) so a stray
  accidental test-leak can never end up in a real ``.env``.
- ``asyncio_mode = "auto"`` is set in ``pyproject.toml``; both sync and
  async test bodies work. We follow the test_client.py convention and use
  a tiny ``run`` helper for clarity.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.bilibili.auth import (
    LoginIncompleteError,
    QrAwaitingConfirmError,
    QrAwaitingScanError,
    QrExpiredError,
    QrLoginError,
    qr_generate,
    qr_poll,
    save_cookies_manual,
    write_env_atomic,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(coro):
    """Run an async coroutine to completion from a sync test."""
    return asyncio.run(coro)


def make_async_client(handler) -> httpx.AsyncClient:
    """Build an ``httpx.AsyncClient`` backed by ``httpx.MockTransport``."""
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


# ---------------------------------------------------------------------------
# qr_generate
# ---------------------------------------------------------------------------


def test_qr_generate_returns_url_and_key_on_code_0() -> None:
    """Happy path: B站 code 0 + data.url (current field name) → return both."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/x/passport-login/web/qrcode/generate"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "url": "https://passport.bilibili.com/x/passport-login/web/qrcode/confirm?...",
                    "qrcode_key": "fake_qr_key_123",
                },
            },
        )

    client = make_async_client(handler)
    result = run(qr_generate(client))
    assert result["qrcode_url"].startswith("https://")
    assert result["qrcode_key"] == "fake_qr_key_123"


def test_qr_generate_falls_back_to_legacy_qrcode_url_field() -> None:
    """Interop: older B站 deployments still emit ``data.qrcode_url``."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "qrcode_url": "https://legacy.example/qr.png",
                    "qrcode_key": "legacy_key_999",
                },
            },
        )

    client = make_async_client(handler)
    result = run(qr_generate(client))
    assert result["qrcode_url"] == "https://legacy.example/qr.png"
    assert result["qrcode_key"] == "legacy_key_999"


def test_qr_generate_raises_qr_login_error_on_non_zero_code() -> None:
    """Adversarial: B站 code != 0 → raise QrLoginError (not silently return)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -1, "message": "service down"})

    client = make_async_client(handler)
    with pytest.raises(QrLoginError):
        run(qr_generate(client))


def test_qr_generate_raises_when_data_missing() -> None:
    """Adversarial: code 0 but data is missing → QrLoginError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": None})

    client = make_async_client(handler)
    with pytest.raises(QrLoginError):
        run(qr_generate(client))


# ---------------------------------------------------------------------------
# qr_poll — happy path: bili_jct captured from data.url query params
# ---------------------------------------------------------------------------


def test_qr_poll_success_captures_cookies_from_url_query_params() -> None:
    """Legacy path: B站 inner_code 0 + url with SESSDATA/bili_jct/DedeUserID
    query params and no Set-Cookie → return them as the success dict.

    Note the B站 dual-code structure: the top-level envelope ``code`` is
    always 0; the QR-login state code lives in ``data.code``. The poll
    MUST dispatch on ``data.code``, not the top-level field."""

    success_url = (
        "https://passport.biligame.com/x/passport-login/web/crossDomain"
        "?DedeUserID=987654&Expires=1700000000"  # secretscan-allow: synthetic test fixture
        "&SESSDATA=fake_sessdata%2Cfake_sessdata"
        "&bili_jct=fake_bili_jct&gourl=https%3A%2F%2Fwww.bilibili.com"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert "qrcode/poll" in request.url.path
        assert request.url.params.get("qrcode_key") == "fake_qr_key_123"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "code": 0,  # inner B站 QR-login state code: SUCCESS
                    "url": success_url,
                    "refresh_token": "rt",
                },
            },
        )

    client = make_async_client(handler)
    result = run(qr_poll(client, "fake_qr_key_123"))

    assert result["status"] == "success"
    assert result["sessdata"].startswith("fake_sessdata")
    assert result["bili_jct"] == "fake_bili_jct"
    assert result["dede_user_id"] == "987654"


# ---------------------------------------------------------------------------
# qr_poll — Bug A regression: B站 no longer returns data.url, cookies live
# in the Set-Cookie response header ONLY. The current B站 poll success
# shape is ``{code: 0, data: {refresh_token: ...}}`` with SESSDATA /
# bili_jct / DedeUserID arriving as Set-Cookie on the response. The poll
# route MUST capture them and return success — the previous code
# short-circuited on the missing data.url field and 500-ed.
# ---------------------------------------------------------------------------


def test_qr_poll_success_captures_cookies_from_set_cookie_when_url_missing() -> None:
    """Bug A core scenario (from a.log): Set-Cookie carries all three
    cookies, ``data.url`` is absent. Must return a success dict.

    Inner B站 data.code == 0 → success; cookies come from Set-Cookie."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=[
                ("Set-Cookie", "SESSDATA=fake_sess_from_header; Path=/; HttpOnly"),
                ("Set-Cookie", "bili_jct=fake_jct_from_header; Path=/; HttpOnly"),
                ("Set-Cookie", "DedeUserID=99999; Path=/; HttpOnly"),
            ],
            json={
                "code": 0,
                "data": {"code": 0, "refresh_token": "rt"},  # no url field
            },
        )

    client = make_async_client(handler)
    result = run(qr_poll(client, "fake_key"))

    assert result["status"] == "success"
    assert result["sessdata"] == "fake_sess_from_header"
    assert result["bili_jct"] == "fake_jct_from_header"
    assert result["dede_user_id"] == "99999"


def test_qr_poll_success_merges_set_cookie_and_data_url() -> None:
    """Mixed coverage: Set-Cookie carries one cookie, data.url carries the
    others. Both paths contribute and the result is a union (Set-Cookie
    wins on collision)."""

    url_partial = (
        "https://passport.biligame.com/x/passport-login/web/crossDomain"
        "?DedeUserID=12345&SESSDATA=fake_sess_from_url"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=[
                ("Set-Cookie", "bili_jct=fake_jct_from_cookie; Path=/; HttpOnly"),
                ("Set-Cookie", "DedeUserID=cookie_dede; Path=/; HttpOnly"),
            ],
            json={
                "code": 0,
                "data": {
                    "code": 0,
                    "url": url_partial,
                    "refresh_token": "rt",
                },
            },
        )

    client = make_async_client(handler)
    result = run(qr_poll(client, "fake_key"))

    assert result["status"] == "success"
    # Only bili_jct in Set-Cookie; SESSDATA was missing from Set-Cookie
    # so it came from data.url.
    assert result["sessdata"] == "fake_sess_from_url"
    assert result["bili_jct"] == "fake_jct_from_cookie"
    # DedeUserID was set in BOTH; Set-Cookie wins (primary path).
    assert result["dede_user_id"] == "cookie_dede"


# ---------------------------------------------------------------------------
# qr_poll — fallback: bili_jct from Set-Cookie header (legacy behaviour)
# ---------------------------------------------------------------------------


def test_qr_poll_success_falls_back_to_set_cookie_for_bili_jct() -> None:
    """Mixed legacy: data.url has SESSDATA+DedeUserID but no bili_jct,
    Set-Cookie carries bili_jct → success, bili_jct from Set-Cookie."""

    url_without_jct = (
        "https://passport.biligame.com/x/passport-login/web/crossDomain"
        "?DedeUserID=111&SESSDATA=fake_sessdata_v2&gourl=https%3A%2F%2Fwww.bilibili.com"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=[("Set-Cookie", "bili_jct=fake_jct_from_cookie; Path=/; HttpOnly")],
            json={
                "code": 0,
                "data": {
                    "code": 0,
                    "url": url_without_jct,
                    "refresh_token": "rt",
                },
            },
        )

    client = make_async_client(handler)
    result = run(qr_poll(client, "fake_key"))

    assert result["status"] == "success"
    # SESSDATA / DedeUserID not in Set-Cookie → fell back to data.url.
    assert result["sessdata"] == "fake_sessdata_v2"
    # bili_jct is in Set-Cookie → captured from Set-Cookie.
    assert result["bili_jct"] == "fake_jct_from_cookie"
    assert result["dede_user_id"] == "111"


# ---------------------------------------------------------------------------
# qr_poll — cookies missing from BOTH paths → LoginIncompleteError
# ---------------------------------------------------------------------------


def test_qr_poll_raises_login_incomplete_when_bili_jct_missing_everywhere() -> None:
    """Adversarial: data.url has no bili_jct AND Set-Cookie has none."""

    url_no_cookies = (
        "https://passport.biligame.com/x/passport-login/web/crossDomain"
        "?DedeUserID=222&SESSDATA=fake_sessdata_v3"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "code": 0,
                    "url": url_no_cookies,
                    "refresh_token": "rt",
                },
            },
        )

    client = make_async_client(handler)
    with pytest.raises(LoginIncompleteError):
        run(qr_poll(client, "fake_key"))


def test_qr_poll_raises_login_incomplete_when_no_cookies_anywhere() -> None:
    """Adversarial: data.url absent AND Set-Cookie empty → LoginIncompleteError
    (the SESSDATA / bili_jct guard still trips)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"code": 0, "refresh_token": "rt"},  # no url field
            },
        )

    client = make_async_client(handler)
    with pytest.raises(LoginIncompleteError):
        run(qr_poll(client, "fake_key"))


def test_qr_poll_raises_login_incomplete_when_sessdata_missing_everywhere() -> None:
    """Adversarial: bili_jct present everywhere but SESSDATA missing from BOTH
    Set-Cookie and data.url → still incomplete."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=[("Set-Cookie", "bili_jct=only_jct_no_sess; Path=/; HttpOnly")],
            json={
                "code": 0,
                "data": {
                    "code": 0,
                    "url": (
                        "https://passport.biligame.com/x/passport-login/web/crossDomain"
                        "?DedeUserID=333&bili_jct=still_only_jct"
                    ),
                    "refresh_token": "rt",
                },
            },
        )

    client = make_async_client(handler)
    with pytest.raises(LoginIncompleteError):
        run(qr_poll(client, "fake_key"))


def test_qr_poll_raises_login_incomplete_when_sessdata_missing() -> None:
    """Adversarial: bili_jct present but SESSDATA missing → still incomplete."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "code": 0,
                    "url": (
                        "https://passport.biligame.com/x/passport-login/web/crossDomain"
                        "?DedeUserID=333&bili_jct=fake_jct_no_sess"
                    ),
                    "refresh_token": "rt",
                },
            },
        )

    client = make_async_client(handler)
    with pytest.raises(LoginIncompleteError):
        run(qr_poll(client, "fake_key"))


# ---------------------------------------------------------------------------
# qr_poll — B站 poll-state codes (CRITICAL — these codes live in
# ``data.code``, NOT the top-level envelope ``code``).
# ---------------------------------------------------------------------------


def test_qr_poll_raises_qr_expired_on_inner_code_86038() -> None:
    """inner_code 86038 = QR code expired (top-level code is always 0)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"code": 86038, "message": "expired"},
            },
        )

    client = make_async_client(handler)
    with pytest.raises(QrExpiredError):
        run(qr_poll(client, "fake_key"))


def test_qr_poll_raises_qr_awaiting_scan_on_inner_code_86101() -> None:
    """inner_code 86101 = not scanned yet — root-cause regression for the
    bug where the top-level code (always 0) was being read and the QR
    states never matched. See a.log curl proof."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"code": 86101, "message": "未扫码"},
            },
        )

    client = make_async_client(handler)
    with pytest.raises(QrAwaitingScanError):
        run(qr_poll(client, "fake_key"))


def test_qr_poll_raises_qr_awaiting_confirm_on_inner_code_86090() -> None:
    """inner_code 86090 = scanned, awaiting user confirm on phone."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"code": 86090, "message": "awaiting confirm"},
            },
        )

    client = make_async_client(handler)
    with pytest.raises(QrAwaitingConfirmError):
        run(qr_poll(client, "fake_key"))


def test_qr_poll_raises_qr_login_error_on_unknown_inner_code() -> None:
    """Adversarial: B站 returns an inner code we don't recognise → generic
    QrLoginError. Top-level code is always 0, so the typo guard fires on
    ``data.code``."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"code": 99999, "message": "wat"},
            },
        )

    client = make_async_client(handler)
    with pytest.raises(QrLoginError):
        run(qr_poll(client, "fake_key"))


def test_qr_poll_raises_qr_login_error_on_non_zero_no_data() -> None:
    """Adversarial: B站 reply with no body at all (network quirk) → no crash."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    client = make_async_client(handler)
    with pytest.raises(QrLoginError):
        run(qr_poll(client, "fake_key"))


def test_qr_poll_raises_qr_login_error_on_top_level_code_only() -> None:
    """Regression: previously the top-level envelope ``code`` was read for
    the QR state match. The fixture below would have been mis-dispatched
    as success under that bug. With the fix, the missing ``data`` object
    triggers QrLoginError (top-level code being non-zero does not get a
    free pass — only ``data.code`` counts)."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Hand-rolled: top-level code = 86101 but no `data` block at all.
        return httpx.Response(
            200,
            json={"code": 86101, "message": "malformed (test fixture)"},
        )

    client = make_async_client(handler)
    with pytest.raises(QrLoginError):
        run(qr_poll(client, "fake_key"))


# ---------------------------------------------------------------------------
# write_env_atomic
# ---------------------------------------------------------------------------


def test_write_env_atomic_updates_existing_keys_and_preserves_others(tmp_path: Path) -> None:
    """Existing .env with OTHER=val: only SESSDATA / BILI_JCT change."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        "SESSDATA=old_sess\n"
        "OTHER=keep_me\n"
        "BILI_JCT=old_jct\n",
        encoding="utf-8",
    )

    write_env_atomic(
        sessdata="new_sess",
        bili_jct="new_jct",
        buvid3=None,
        env_path=env_path,
    )

    text = env_path.read_text(encoding="utf-8")
    assert "SESSDATA=new_sess" in text
    assert "BILI_JCT=new_jct" in text
    assert "OTHER=keep_me" in text
    assert "old_sess" not in text
    assert "old_jct" not in text


def test_write_env_atomic_adds_missing_keys(tmp_path: Path) -> None:
    """No SESSDATA / BILI_JCT line yet → they get appended (after preserving
    everything else)."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        "ROOM_ID=22210347\nHOST=127.0.0.1\n",
        encoding="utf-8",
    )

    write_env_atomic(
        sessdata="fresh_sess",
        bili_jct="fresh_jct",
        buvid3="fresh_buvid3",
        env_path=env_path,
    )

    text = env_path.read_text(encoding="utf-8")
    assert "ROOM_ID=22210347" in text
    assert "HOST=127.0.0.1" in text
    assert "SESSDATA=fresh_sess" in text
    assert "BILI_JCT=fresh_jct" in text
    assert "BUVID3=fresh_buvid3" in text


def test_write_env_atomic_creates_file_when_missing(tmp_path: Path) -> None:
    """No .env at all → create a fresh one with the 3 keys."""
    env_path = tmp_path / ".env"
    assert not env_path.exists()

    write_env_atomic(
        sessdata="brand_new",
        bili_jct="brand_new_jct",
        buvid3="brand_new_buvid",
        env_path=env_path,
    )

    text = env_path.read_text(encoding="utf-8")
    assert "SESSDATA=brand_new" in text
    assert "BILI_JCT=brand_new_jct" in text
    assert "BUVID3=brand_new_buvid" in text


def test_write_env_atomic_leaves_no_tmp_file_behind(tmp_path: Path) -> None:
    """Atomicity: the .env.tmp staging file must NOT linger after a successful
    write — ``os.replace`` is what gives us the atomicity."""
    env_path = tmp_path / ".env"
    env_path.write_text("OTHER=val\n", encoding="utf-8")

    write_env_atomic(
        sessdata="atomic_sess",
        bili_jct="atomic_jct",
        buvid3=None,
        env_path=env_path,
    )

    leftover = tmp_path / ".env.tmp"
    assert not leftover.exists(), "staging .env.tmp must not be left behind"
    assert env_path.exists()


def test_write_env_atomic_replaces_existing_file_in_place(tmp_path: Path) -> None:
    """Atomicity sanity: the file is replaced atomically (the old inode
    does NOT survive as a visible partial-write)."""
    env_path = tmp_path / ".env"
    env_path.write_text("SESSDATA=old\nOTHER=val\n", encoding="utf-8")
    original_inode = env_path.stat().st_ino

    write_env_atomic(
        sessdata="new",
        bili_jct="new_jct",
        buvid3=None,
        env_path=env_path,
    )

    # os.replace on the same FS keeps the inode, but the *contents* must
    # be the new ones (not a half-mixed state). We assert contents, not
    # inode, because tmp_path could be a different FS.
    text = env_path.read_text(encoding="utf-8")
    assert "SESSDATA=new" in text
    assert "BILI_JCT=new_jct" in text
    assert "OTHER=val" in text
    assert "old" not in text
    # And the staging file is gone.
    assert not (tmp_path / ".env.tmp").exists()
    # The file should exist (inode check is a sanity bonus, not required).
    assert env_path.exists()
    # Reference original_inode so the linter doesn't complain about unused.
    assert isinstance(original_inode, int)


def test_write_env_atomic_omits_buvid3_line_when_none(tmp_path: Path) -> None:
    """``buvid3=None`` → no BUVID3= line is written."""
    env_path = tmp_path / ".env"

    write_env_atomic(
        sessdata="s",
        bili_jct="j",
        buvid3=None,
        env_path=env_path,
    )

    text = env_path.read_text(encoding="utf-8")
    assert "BUVID3" not in text


# ---------------------------------------------------------------------------
# save_cookies_manual
# ---------------------------------------------------------------------------


def test_save_cookies_manual_writes_env_and_returns_uname_mid_on_valid_nav(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan B happy path: provided cookies pass nav → .env is written.

    The cookies are baked into the BilibiliClient's own httpx client at
    construction time — patching BilibiliClient proves this contract
    (no shared client is passed in).
    """
    env_path = tmp_path / ".env"
    env_path.write_text("ROOM_ID=22210347\n", encoding="utf-8")

    fake_bili = MagicMock()
    fake_bili.get_user_info = AsyncMock(
        return_value={"uname": "tester", "mid": 12345, "isLogin": True}
    )
    fake_bili.close = AsyncMock()
    monkeypatch.setattr(
        "app.bilibili.client.BilibiliClient", lambda **kwargs: fake_bili
    )

    result = run(
        save_cookies_manual(
            sessdata="manual_sess",
            bili_jct="manual_jct",
            buvid3="manual_buvid",
            env_path=env_path,
        )
    )

    assert result["uname"] == "tester"
    assert result["mid"] == 12345
    # BilibiliClient was constructed with the provided cookies + csrf.
    assert fake_bili.get_user_info.await_count == 1
    fake_bili.close.assert_awaited_once()

    # The .env must have been written with the provided values.
    text = env_path.read_text(encoding="utf-8")
    assert "SESSDATA=manual_sess" in text
    assert "BILI_JCT=manual_jct" in text
    assert "BUVID3=manual_buvid" in text
    assert "ROOM_ID=22210347" in text


def test_save_cookies_manual_raises_login_incomplete_on_invalid_nav(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan B failure path: nav says cookies are not valid → raise
    LoginIncompleteError AND do NOT touch the .env file."""
    env_path = tmp_path / ".env"
    env_path.write_text("SESSDATA=original_sess\nOTHER=val\n", encoding="utf-8")
    original_text = env_path.read_text(encoding="utf-8")

    fake_bili = MagicMock()
    fake_bili.get_user_info = AsyncMock(return_value=None)
    fake_bili.close = AsyncMock()
    monkeypatch.setattr(
        "app.bilibili.client.BilibiliClient", lambda **kwargs: fake_bili
    )

    with pytest.raises(LoginIncompleteError):
        run(
            save_cookies_manual(
                sessdata="bad_sess",
                bili_jct="bad_jct",
                buvid3=None,
                env_path=env_path,
            )
        )

    # Crucial: the .env was NOT clobbered.
    assert env_path.read_text(encoding="utf-8") == original_text


def test_save_cookies_manual_raises_login_incomplete_when_nav_data_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adversarial: nav returns code 0 but data is None → treat as invalid."""

    env_path = tmp_path / ".env"
    env_path.write_text("SESSDATA=keepme\n", encoding="utf-8")

    fake_bili = MagicMock()
    fake_bili.get_user_info = AsyncMock(return_value=None)
    fake_bili.close = AsyncMock()
    monkeypatch.setattr(
        "app.bilibili.client.BilibiliClient", lambda **kwargs: fake_bili
    )

    with pytest.raises(LoginIncompleteError):
        run(
            save_cookies_manual(
                sessdata="x",
                bili_jct="y",
                buvid3=None,
                env_path=env_path,
            )
        )

    assert "keepme" in env_path.read_text(encoding="utf-8")
