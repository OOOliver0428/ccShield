"""Typed B站 (Bilibili) HTTP API client.

This module wraps the B站 web / live APIs used by reccshield for:

- user login check (`/x/web-interface/nav`)
- room init / room info / short-id → real-id translation
- danmaku WebSocket token (`/xlive/web-room/v1/index/getDanmuInfo`) — only
  endpoint that uses WBI signing
- ban / unban write APIs (csrf = `bili_jct` cookie)
- paginated ban-list read

Error mapping:
    body["code"] == 0 → success
    -101 → AuthExpiredError
    -403 → PermissionDeniedError
    -509 → RateLimitedError
    other → BiliApiError

Design notes:
- All methods are async, all return typed values or raise typed exceptions.
- The class owns an `httpx.AsyncClient` for production use, but tests can
  inject their own via `BilibiliClient(client=...)` for full mock-control.
- WBI is used only by `get_danmu_info`. The WBI signer is the module-level
  `wbi_signer` from `app.bilibili.wbi`.
- `csrf_token` (bili_jct) is lazily resolved from
  `app.config.settings.BILI_JCT` if not provided at construction. The
  lazy import is intentional: `app.config.py` is owned by T2 (parallel);
  this module must remain importable while T2 is still in flight.
- `get_ban_list` paginates all pages with a high safety cap to bound work.
  It does NOT consult any external room-state (T17 owns that wrapping).
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
from loguru import logger

from app.bilibili.exceptions import (
    AuthExpiredError,
    BiliApiError,
    PermissionDeniedError,
    RateLimitedError,
)
from app.bilibili.wbi import WbiSigner, wbi_signer

# ---------------------------------------------------------------------------
# Endpoints & headers
# ---------------------------------------------------------------------------

_NAV_URL: str = "https://api.bilibili.com/x/web-interface/nav"
_LIVE_BASE_URL: str = "https://api.live.bilibili.com"

_DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://live.bilibili.com",
    "Origin": "https://live.bilibili.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}

# B站 currently returns ten silent-user rows per page. Real rooms can easily
# exceed the old ten-page cap (room 1601605 reported 21 pages / 201 rows), so
# keep the runaway-response guard without truncating ordinary moderation
# lists at 100 rows.
_BAN_LIST_MAX_PAGES: int = 100


# ---------------------------------------------------------------------------
# Lazy settings lookup
# ---------------------------------------------------------------------------


def _load_settings_or_default() -> tuple[dict[str, str], str]:
    """Resolve cookies + csrf from `app.config.settings`, falling back to empty.

    `app.config.py` is owned by T2 (parallel worker). This module needs to
    remain importable even when T2 hasn't landed. We attempt a lazy import
    each call so newly-landed settings take effect without restarting.
    """
    try:
        from app.config import settings  # lazy: T2 owns config.py (parallel)

        cookies = dict(settings.cookies) if hasattr(settings, "cookies") else {}
        csrf = str(getattr(settings, "BILI_JCT", "") or "")
        return cookies, csrf
    except (ImportError, AttributeError):
        return {}, ""


def _raise_for_business_code(
    code: int, message: str, *, allow_none: bool = False
) -> None:
    """Map B站 body code to typed exception.

    Args:
        code: the `code` from the B站 JSON envelope.
        message: the `message` from the body.
        allow_none: when True and code is non-zero, return None instead of
            raising — only used for the nav "check-login" flow that needs to
            detect expired cookies without raising.
    """
    if code == 0:
        return
    if allow_none:
        return
    if code == -101:
        raise AuthExpiredError(message)
    if code == -403:
        raise PermissionDeniedError(message)
    if code == -509:
        raise RateLimitedError(message)
    raise BiliApiError(code=code, message=message)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class BilibiliClient:
    """Typed wrapper around `httpx.AsyncClient` for the B站 endpoints used
    by reccshield.

    Constructor parameters are keyword-only; the defaults read from
    `app.config.settings` lazily so the module stays usable while T2 lands
    in parallel.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        cookies: dict[str, str] | None = None,
        csrf_token: str | None = None,
        timeout: float = 30.0,
        signer: WbiSigner | None = None,
    ) -> None:
        settings_cookies, settings_csrf = _load_settings_or_default()
        resolved_cookies = cookies if cookies is not None else settings_cookies
        resolved_csrf = csrf_token if csrf_token is not None else settings_csrf

        if client is None:
            self._owns_client = True
            self._client = httpx.AsyncClient(
                cookies=resolved_cookies,
                headers=_DEFAULT_HEADERS,
                timeout=timeout,
            )
        else:
            self._owns_client = False
            self._client = client

        self._csrf_token: str = resolved_csrf
        self._cookies: dict[str, str] = dict(resolved_cookies)
        self._signer: WbiSigner = signer if signer is not None else wbi_signer

    @property
    def http(self) -> httpx.AsyncClient:
        """Expose the underlying client (for tests / direct calls)."""
        return self._client

    @property
    def csrf_token(self) -> str:
        """The csrf (bili_jct) used for write endpoints."""
        return self._csrf_token

    @property
    def signer(self) -> WbiSigner:
        """The WBI signer used by `get_danmu_info`."""
        return self._signer

    def get_cookie(self, name: str) -> str | None:
        """Return one mirrored cookie without exposing the credential map."""
        value = self._cookies.get(name)
        return value if value else None

    async def close(self) -> None:
        """Close the underlying client.

        Always closes — even an injected client — so callers can rely on
        `await client.close()` to release resources. If you need a shared
        client across instances, do NOT call `close()` on the wrapping
        `BilibiliClient` and manage the `httpx.AsyncClient` yourself.
        """
        await self._client.aclose()

    def update_cookies(self, cookies: dict[str, str]) -> None:
        """Refresh the underlying httpx cookie jar in place.

        The process-wide ``BilibiliClient`` is constructed at module-import
        time when ``.env`` is typically empty, so its httpx cookie jar is
        empty. After a successful QR / manual login mutates
        ``app.config.settings`` in place, the existing client's jar MUST
        be refreshed — rebuilding the client would discard the keep-alive
        connection pool and re-open TCP connections.

        Per-cookie upsert: entries that share a key are replaced, others
        are preserved (httpx ``Cookies.update`` semantics).

        Args:
            cookies: cookie name → value pairs to push into the jar. Empty
                dict is a no-op (the current state is left intact).
        """
        self._client.cookies.update(cookies)
        self._cookies.update(cookies)
        # Write APIs sign their form with the cached CSRF value. A QR or
        # manual login can replace ``bili_jct`` after this long-lived client
        # was constructed, so refreshing only the HTTP cookie jar would make
        # reads succeed while ban/unban still used the stale token.
        refreshed_csrf = cookies.get("bili_jct")
        if refreshed_csrf:
            self._csrf_token = refreshed_csrf

    # -----------------------------------------------------------------------
    # Read APIs
    # -----------------------------------------------------------------------

    async def get_user_info(self) -> dict[str, Any] | None:
        """GET `/x/web-interface/nav`. Returns the `data` payload on success.

        Returns `None` on a non-zero business code so the auth flow can detect
        expired cookies (`-101`) without an exception.
        """
        response = await self._client.get(_NAV_URL, headers=_DEFAULT_HEADERS)
        body = self._parse_body(response)
        code = body.get("code", -1)
        if code != 0:
            logger.warning(
                "get_user_info: non-zero nav code={} message={}",
                code,
                body.get("message"),
            )
            return None
        data = body.get("data")
        return data if isinstance(data, dict) else None

    async def get_room_init(self, room_id: int) -> dict[str, Any] | None:
        """GET `/room/v1/Room/room_init` — short-id → real-id translation.

        Returns the `data` payload on code 0, else None.
        """
        url = f"{_LIVE_BASE_URL}/room/v1/Room/room_init"
        response = await self._client.get(
            url, params={"id": room_id}, headers=_DEFAULT_HEADERS
        )
        body = self._parse_body(response)
        return self._extract_data_or_none(body, "get_room_init")

    async def get_room_info(self, room_id: int) -> dict[str, Any] | None:
        """GET `/room/v1/Room/get_info`.

        Per T4 spec we deliberately do NOT call `_get_anchor_name` like
        ccShield did (a redundant double-fetch). If the caller wants the
        anchor name they can fetch lazily elsewhere.
        """
        url = f"{_LIVE_BASE_URL}/room/v1/Room/get_info"
        response = await self._client.get(
            url, params={"room_id": room_id}, headers=_DEFAULT_HEADERS
        )
        body = self._parse_body(response)
        return self._extract_data_or_none(body, "get_room_info")

    async def get_anchor_info(self, uid: int) -> dict[str, Any] | None:
        """Return the public profile of a live-room anchor.

        ``Room/get_info`` identifies the anchor by uid but does not reliably
        include their display name. ``Master/info`` supplies that name under
        ``data.info.uname`` without connecting to the room or its danmaku WS.
        """
        url = f"{_LIVE_BASE_URL}/live_user/v1/Master/info"
        response = await self._client.get(
            url, params={"uid": uid}, headers=_DEFAULT_HEADERS
        )
        body = self._parse_body(response)
        data = self._extract_data_or_none(body, "get_anchor_info")
        if not isinstance(data, dict):
            return None
        info = data.get("info")
        return info if isinstance(info, dict) else None

    async def get_active_super_chats(
        self, room_id: int
    ) -> list[dict[str, Any]]:
        """Return SCs that are already active when a room is connected.

        The public WebSocket only guarantees future events. B站's room
        bootstrap response carries the currently pinned SC cards under
        ``data.super_chat_info.message_list``; seeding from it prevents a
        still-active paid message from disappearing merely because this app
        connected after the purchase event.
        """
        url = f"{_LIVE_BASE_URL}/xlive/web-room/v1/index/getInfoByRoom"
        params = {"room_id": str(room_id)}
        body: dict[str, Any] = {}
        for attempt in range(2):
            signed = await self._signer.sign(self._client, params)
            response = await self._client.get(
                url, params=signed, headers=_DEFAULT_HEADERS
            )
            body = self._parse_body(response)
            code = body.get("code", -1)
            if code == 0:
                break
            if code == -352 and attempt == 0:
                logger.warning(
                    "get_active_super_chats: -352, refreshing WBI keys"
                )
                self._signer.last_update = 0.0
                continue
            logger.warning(
                "get_active_super_chats: code={} msg={}",
                code,
                body.get("message"),
            )
            return []

        data = self._extract_data_or_none(body, "get_active_super_chats")
        if not isinstance(data, dict):
            return []
        super_chat_info = data.get("super_chat_info")
        if not isinstance(super_chat_info, dict):
            return []
        message_list = super_chat_info.get("message_list")
        if not isinstance(message_list, list):
            return []
        return [item for item in message_list if isinstance(item, dict)]

    async def resolve_room_id(
        self, input_id: int
    ) -> dict[str, Any] | None:
        """Resolve a user-supplied room id to a canonical record.

        Strategy:
          1. Try `get_room_info(input_id)`. If it returns data with
             `room_id`, treat `input_id` as the real room id and enrich with
             `get_room_init`.
          2. Otherwise, try `get_room_init(input_id)` to translate as a
             short id. If translation succeeds, fetch full info using the
             translated real id and merge in `uid`/`short_id`.

        Returns None if both attempts fail.
        """
        room_info = await self.get_room_info(input_id)
        if isinstance(room_info, dict) and room_info.get("room_id"):
            init = await self.get_room_init(input_id)
            uid = init.get("uid") if isinstance(init, dict) else None
            if not isinstance(uid, int):
                uid = room_info.get("uid")
            short_id = (
                init.get("short_id", 0) if isinstance(init, dict) else 0
            )
            uname = room_info.get("uname")
            if not isinstance(uname, str) or not uname.strip():
                anchor_info = (
                    await self.get_anchor_info(uid)
                    if isinstance(uid, int) and uid > 0
                    else None
                )
                uname = (
                    anchor_info.get("uname", "")
                    if isinstance(anchor_info, dict)
                    else ""
                )
            return {
                **room_info,
                "uid": uid,
                "short_id": short_id,
                "uname": uname if isinstance(uname, str) else "",
                "is_short_id": False,
            }

        init = await self.get_room_init(input_id)
        if not (isinstance(init, dict) and init.get("room_id")):
            logger.error("resolve_room_id: cannot resolve {}", input_id)
            return None

        real_room_id = init["room_id"]
        full_info = await self.get_room_info(real_room_id)
        if full_info:
            uid = init.get("uid")
            if not isinstance(uid, int):
                uid = full_info.get("uid")
            uname = full_info.get("uname")
            if not isinstance(uname, str) or not uname.strip():
                anchor_info = (
                    await self.get_anchor_info(uid)
                    if isinstance(uid, int) and uid > 0
                    else None
                )
                uname = (
                    anchor_info.get("uname", "")
                    if isinstance(anchor_info, dict)
                    else ""
                )
            return {
                **full_info,
                "uid": uid,
                "short_id": init.get("short_id", 0),
                "uname": uname if isinstance(uname, str) else "",
                "is_short_id": True,
                "input_id": input_id,
            }

        # Only room_init present (no enrich)
        return {
            "room_id": real_room_id,
            "uid": init.get("uid"),
            "short_id": init.get("short_id", 0),
            "live_status": init.get("live_status", 0),
            "is_short_id": True,
            "input_id": input_id,
            "uname": "",
        }

    async def get_danmu_info(self, room_id: int) -> dict[str, Any]:
        """GET `/xlive/web-room/v1/index/getDanmuInfo` — WBI-signed.

        The only endpoint in this client that uses WBI signing. On a -352
        ("wbi stale") response, the keys are refreshed via the module-level
        `wbi_signer` and the request retried **exactly once**.

        Raises:
            AuthExpiredError / PermissionDeniedError / RateLimitedError /
                BiliApiError — mapped from the response's `code`.
        """
        url = f"{_LIVE_BASE_URL}/xlive/web-room/v1/index/getDanmuInfo"

        signed = await self._signer.sign(
            self._client, {"id": str(room_id), "type": "0"}
        )
        response = await self._client.get(
            url, params=signed, headers=_DEFAULT_HEADERS
        )
        body = self._parse_body(response)
        code = body.get("code", -1)
        if code == 0:
            data = body.get("data")
            if not isinstance(data, dict):
                raise BiliApiError(code=0, message="getDanmuInfo: missing data")
            return data

        if code != -352:
            self._raise_for_code(code, body.get("message"))

        logger.warning("getDanmuInfo: -352, refreshing WBI keys and retrying once")
        self._signer.last_update = 0.0
        signed = await self._signer.sign(
            self._client, {"id": str(room_id), "type": "0"}
        )
        response = await self._client.get(
            url, params=signed, headers=_DEFAULT_HEADERS
        )
        body = self._parse_body(response)
        retry_code = body.get("code", -1)
        if retry_code == 0:
            data = body.get("data")
            if not isinstance(data, dict):
                raise BiliApiError(
                    code=0, message="getDanmuInfo: missing data after retry"
                )
            return data
        self._raise_for_code(retry_code, body.get("message"))
        raise AssertionError("unreachable: _raise_for_code must raise")

    # -----------------------------------------------------------------------
    # Write APIs (ban / unban) — csrf = bili_jct
    # -----------------------------------------------------------------------

    async def ban_user(
        self,
        room_id: int,
        uid: int,
        hour: int,
        msg: str = "",
    ) -> bool:
        """Ban ``uid`` for the current stream or a fixed number of hours.

        Returns `True` on `code == 0`. Raises a typed exception on any
        non-zero business code. ``hour=0`` means the current stream only;
        positive values are fixed-hour bans.

        Bilibili's current live-room web client uses ``AddSilentUser`` for all
        duration levels. Session-only bans use ``type=2, hour=0``; timed and
        permanent bans use ``type=1`` with a positive hour value or ``-1``.
        ``msg`` remains part of our public method for local evidence/API
        compatibility; the upstream request does not accept it.
        """
        url = f"{_LIVE_BASE_URL}/xlive/web-ucenter/v1/banned/AddSilentUser"
        form: dict[str, str] = {
            "room_id": str(room_id),
            "tuid": str(uid),
            "mobile_app": "web",
            "type": "2" if hour == 0 else "1",
            "hour": str(int(hour)),
            "csrf_token": self._csrf_token,
            "csrf": self._csrf_token,
        }
        response = await self._client.post(
            url, data=form, headers=_DEFAULT_HEADERS
        )
        body = self._parse_body(response)
        code = body.get("code", -1)
        if code == 0:
            logger.info(
                "ban ok room={} uid={} hour={}", room_id, uid, hour
            )
            return True
        logger.error("ban failed: {}", body)
        self._raise_for_code(code, body.get("message"))
        return False  # unreachable; satisfies type checker

    async def unban_user(self, room_id: int, block_id: int) -> bool:
        """POST `/banned_service/v1/Silent/del_room_block_user`. Returns True."""
        url = (
            f"{_LIVE_BASE_URL}/banned_service/v1/Silent/del_room_block_user"
        )
        form: dict[str, str] = {
            "roomid": str(room_id),
            "id": str(block_id),
            "csrf_token": self._csrf_token,
            "csrf": self._csrf_token,
            "visit_id": "",
        }
        response = await self._client.post(
            url, data=form, headers=_DEFAULT_HEADERS
        )
        body = self._parse_body(response)
        code = body.get("code", -1)
        if code == 0:
            logger.info(
                "unban ok room={} block_id={}", room_id, block_id
            )
            return True
        logger.error("unban failed: {}", body)
        self._raise_for_code(code, body.get("message"))
        return False  # unreachable

    # -----------------------------------------------------------------------
    # Read paginated — ban list
    # -----------------------------------------------------------------------

    async def get_ban_list(
        self,
        room_id: int,
        page_size: int = 50,
        *,
        is_running: Callable[[], bool] | None = None,
    ) -> list[dict[str, Any]]:
        """Paginate `/xlive/web-ucenter/v1/banned/GetSilentUserList`.

        Stops at `total_page` (or after `_BAN_LIST_MAX_PAGES`, whichever is
        smaller) so we always bound work even when the server reports a
        wild `total_page`.

        Args:
            room_id: real room id.
            page_size: retained for API compatibility. This endpoint fixes
                the page size server-side; its `ps` field is the page number.
            is_running: optional callback invoked once per page; if it
                returns False, pagination stops. Wired by T17 (which owns
                banlist) — kept optional so the MVP stays simple.

        Returns:
            Flat list of ban entries (b站 page-wrapped `data.data`).
        """
        url = (
            f"{_LIVE_BASE_URL}/xlive/web-ucenter/v1/banned/GetSilentUserList"
        )
        collected: list[dict[str, Any]] = []
        current_page = 1

        while current_page <= _BAN_LIST_MAX_PAGES:
            if is_running is not None and not is_running():
                logger.info(
                    "get_ban_list: room {} stopped (callback)", room_id
                )
                break

            form: dict[str, str] = {
                "room_id": str(room_id),
                "ps": str(current_page),
                "csrf": self._csrf_token,
                "csrf_token": self._csrf_token,
                "visit_id": "",
            }
            response = await self._client.post(
                url, data=form, headers=_DEFAULT_HEADERS
            )
            text = response.text
            if not text:
                logger.warning(
                    "get_ban_list: empty body page={} room={}",
                    current_page,
                    room_id,
                )
                break

            try:
                body = response.json()
            except json.JSONDecodeError:
                logger.warning(
                    "get_ban_list: non-JSON page={} room={} body={!r}",
                    current_page,
                    room_id,
                    text[:200],
                )
                break

            code = body.get("code", -1)
            if code != 0:
                logger.warning(
                    "get_ban_list: code={} msg={}", code, body.get("message")
                )
                self._raise_for_code(code, body.get("message"))
                break  # unreachable

            payload = body.get("data") or {}
            page_items = payload.get("data") or []
            if not page_items:
                break
            total = payload.get("total", 0)
            total_page = payload.get("total_page", 1)

            collected.extend(page_items)
            logger.info(
                "get_ban_list page={} got={} total={} total_page={}",
                current_page,
                len(page_items),
                total,
                total_page,
            )

            if current_page >= total_page or len(collected) >= total:
                break
            if current_page >= _BAN_LIST_MAX_PAGES:
                logger.warning(
                    "get_ban_list: truncating room={} at safety cap={} "
                    "reported_total_page={} reported_total={}",
                    room_id,
                    _BAN_LIST_MAX_PAGES,
                    total_page,
                    total,
                )
                break
            current_page += 1

        logger.info("get_ban_list done: {} entries", len(collected))
        return collected

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _parse_body(self, response: httpx.Response) -> dict[str, Any]:
        """Parse the B站 JSON body with a graceful fallback to `{}`.

        We do NOT raise on HTTP errors here; the B站 endpoints always
        reply 200 with a JSON envelope carrying `code`. Network/HTTP errors
        propagate naturally so the caller sees a real connection problem.
        """
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise BiliApiError(
                code=-1, message=f"invalid JSON body: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise BiliApiError(code=-1, message="response body is not an object")
        return data

    def _extract_data_or_none(
        self, body: dict[str, Any], op: str
    ) -> dict[str, Any] | None:
        code = body.get("code", -1)
        if code != 0:
            logger.warning(
                "{}: code={} msg={}", op, code, body.get("message")
            )
            return None
        data = body.get("data")
        return data if isinstance(data, dict) else None

    def _raise_for_code(self, code: int, message: str | None) -> None:
        _raise_for_business_code(code, message or f"Bili error {code}")


__all__ = [
    "BilibiliClient",
]


def _module_level_bili_client() -> BilibiliClient:
    """Lazy module-level factory — do NOT construct at import time so the
    module stays importable while `app.config.py` is still being created.
    Tests and the application entry-point call this on demand.
    """
    return BilibiliClient()
