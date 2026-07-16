"""B站 (Bilibili) QR-code login + manual-fallback cookie persistence.

This module owns the QR-code login flow that ccShield uses to obtain
``SESSDATA`` / ``bili_jct`` / ``BUVID3`` cookies without a real browser:

1. ``qr_generate`` — ``GET /x/passport-login/web/qrcode/generate``
   returns ``{qrcode_url, qrcode_key}``. The caller renders the QR for the
   user to scan with the B站 mobile app.

2. ``qr_poll`` — ``GET /x/passport-login/web/qrcode/poll?qrcode_key=...``
   is polled on a short interval. B站 returns one of:
       code 86101 → not scanned yet           → ``QrAwaitingScanError``
       code 86090 → scanned, awaiting confirm → ``QrAwaitingConfirmError``
       code 86038 → QR expired                → ``QrExpiredError``
       code 0     → login complete             → success dict
       other     → ``QrLoginError``

3. On success (code 0), the auth cookies live in EITHER ``data.url`` query
   params (legacy B站 format) OR the ``Set-Cookie`` response header
   (current B站 format — they stopped returning ``data.url`` sometime in
   2024). We capture each of ``SESSDATA`` / ``bili_jct`` /
   ``DedeUserID`` from ``Set-Cookie`` FIRST and fall back to
   ``data.url``'s query params for any cookie still missing. The
   ``data.url`` field is NO LONGER required to exist: a successful
   cookie-only-in-Set-Cookie response is now a valid login. If both
   paths are empty for ``SESSDATA`` or ``bili_jct``,
   ``LoginIncompleteError`` is raised — we never persist a partial
   credential set.

4. ``write_env_atomic`` persists the three cookies to ``.env`` by writing
   a ``.env.tmp`` staging file first, then ``os.replace``-ing it into
   place. A crash mid-write cannot leave a half-written ``.env`` visible.

5. ``save_cookies_manual`` is the Plan B fallback: a user pastes their
   own SESSDATA/bili_jct/buvid3 (e.g. extracted from a browser DevTools
   tab). We put them on a fresh ``BilibiliClient``, call ``/nav`` to
   validate, and only then persist. On invalid nav → raise
   ``LoginIncompleteError`` AND leave the existing ``.env`` untouched.

Design constraints
------------------
- No FastAPI or HTTP routes live here; the API package owns the route layer. This module
  exposes pure async functions + one sync helper.
- ``httpx.AsyncClient`` is always passed in (DI). The caller controls
  transport / headers / timeouts and the tests inject a
  ``httpx.MockTransport``.
- Typed strictly: every parameter and return value carries an explicit
  type. ``Any`` is NOT used.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from loguru import logger

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

_QR_GENERATE_URL: str = (
    "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
)
_QR_POLL_URL: str = (
    "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
)
# /x/frontend/finger/spi returns a device fingerprint (b_3 == buvid3).
# B站's QR-login flow does NOT Set-Cookie buvid3 itself; we call this
# endpoint right after a successful QR poll to capture the fingerprint
# the browser would normally have set. Needed for WBI-signed endpoints
# (e.g. /xlive/web-room/v1/index/getDanmuInfo).
_BUVID3_SPI_URL: str = "https://api.bilibili.com/x/frontend/finger/spi"

# B站 QR-poll state codes (NOT the generic API envelope codes).
_QR_CODE_SCAN_AWAITING: int = 86101   # not scanned yet
_QR_CODE_CONFIRM_AWAITING: int = 86090  # scanned, awaiting user confirm on phone
_QR_CODE_EXPIRED: int = 86038         # QR expired (must regenerate)

# Default User-Agent / Referer — B站's passport endpoints reject requests
# without a plausible browser UA.
_QR_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://passport.bilibili.com",
    "Origin": "https://passport.bilibili.com",
}

# .env keys we own — every other line is preserved untouched.
_ENV_KEYS_OWNED: tuple[str, ...] = ("SESSDATA", "BILI_JCT", "BUVID3")

# Filename of the atomic-write staging file (sibling of the target .env).
_ENV_TMP_SUFFIX: str = ".tmp"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class QrLoginError(Exception):
    """Base class for all B站 QR-login errors.

    Carries the B站 response ``code`` (int | None) and the human-readable
    ``message``. Subclasses cover the four well-known poll-state codes.
    """


class QrExpiredError(QrLoginError):
    """B站 code 86038: the QR code has expired and must be regenerated."""

    def __init__(self, message: str = "qr code expired") -> None:
        super().__init__(message)


class QrAwaitingScanError(QrLoginError):
    """B站 code 86101: the user has not scanned the QR yet."""

    def __init__(self, message: str = "awaiting scan") -> None:
        super().__init__(message)


class QrAwaitingConfirmError(QrLoginError):
    """B站 code 86090: scanned, awaiting user confirm on the phone."""

    def __init__(self, message: str = "awaiting confirm") -> None:
        super().__init__(message)


class LoginIncompleteError(QrLoginError):
    """Login was reported successful, but a required cookie is missing.

    This is the "bili_jct missing from BOTH url and Set-Cookie" / "SESSDATA
    missing" / "data.url absent" failure mode. We deliberately do NOT
    persist partial credentials — re-raise and let the caller decide
    whether to retry, fall back to manual entry, or surface to the user.
    """

    def __init__(self, message: str = "login incomplete: missing required cookie") -> None:
        super().__init__(message)


# ---------------------------------------------------------------------------
# JSON envelope parsing
# ---------------------------------------------------------------------------


def _parse_json_object(text: str) -> dict[str, object]:
    """Parse the B站 JSON envelope (``{code, message, data}``) defensively.

    Returns ``{}`` on empty / non-JSON / non-object bodies so the caller can
    branch on ``code`` without try/except. Network/HTTP errors already
    raised by ``httpx`` are not swallowed — we only handle malformed bodies.
    """
    if not text:
        return {}
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ---------------------------------------------------------------------------
# Cookie capture on poll success
# ---------------------------------------------------------------------------


def _first_query_value(params: object, key: str) -> str:
    """Return the first ``params[key]`` entry if it is a non-empty string."""
    if not isinstance(params, dict):
        return ""
    raw: object = params.get(key)
    if not isinstance(raw, list) or not raw:
        return ""
    first: object = raw[0]
    return first if isinstance(first, str) else ""


def _url_query_params(url: str) -> dict[str, list[str]]:
    """Parse ``url``'s query string into a ``{name: [values]}`` dict.

    Empty / non-string URLs → empty dict (treated as "no fallback data").
    Used to extract SESSDATA / bili_jct / DedeUserID from B站's legacy
    ``data.url`` field when Set-Cookie did not carry them.
    """
    if not isinstance(url, str) or not url:
        return {}
    parsed = urlparse(url)
    if not parsed.query:
        return {}
    return parse_qs(parsed.query)


def _cookie_value(response: httpx.Response, name: str) -> str:
    """Pull a single cookie value from the response's ``Set-Cookie`` jar.

    Returns ``""`` when the jar is not present or has no entry for
    ``name``. ``httpx.Response.cookies`` is a ``http.cookies.SimpleCookie``
    that already strips ``Path=``/``HttpOnly``/etc. and yields the raw
    value via ``.get(name)``.
    """
    jar: object = getattr(response, "cookies", None)
    if jar is None or not hasattr(jar, "get"):
        return ""
    raw: object = jar.get(name)  # type: ignore[attr-defined]
    return raw if isinstance(raw, str) else ""


def _capture_success_cookies(response: httpx.Response) -> dict[str, str]:
    """Pull SESSDATA / bili_jct / DedeUserID from B站's success response.

    Strategy (dual-path, per-cookie):
      1. PRIMARY: ``response.cookies`` (the Set-Cookie header jar) — the
         current B站 format returns the three cookies as Set-Cookie
         headers and no longer populates ``data.url``.
      2. FALLBACK: parse the ``data.url`` query params (legacy B站
         format) for any cookie still missing after Set-Cookie.

    Per-cookie union: each cookie name is resolved independently, so a
    partial Set-Cookie (only ``bili_jct``) plus a partial ``data.url``
    (SESSDATA + DedeUserID) merges into a complete credential set.

    Returns: ``{sessdata, bili_jct, dede_user_id}`` — each value is
    ``""`` when neither path yielded a non-empty match.
    """
    data_url: str = ""
    body = _parse_json_object(response.text)
    body_data = body.get("data") if isinstance(body, dict) else None
    if isinstance(body_data, dict):
        url_obj = body_data.get("url")
        if isinstance(url_obj, str):
            data_url = url_obj
    url_params: dict[str, list[str]] = _url_query_params(data_url)

    sessdata = _cookie_value(response, "SESSDATA") or _first_query_value(
        url_params, "SESSDATA"
    )
    bili_jct = _cookie_value(response, "bili_jct") or _first_query_value(
        url_params, "bili_jct"
    )
    dede_user_id = _cookie_value(response, "DedeUserID") or _first_query_value(
        url_params, "DedeUserID"
    )

    # Diagnostic: which path contributed which cookie? Distinguishes
    # "B站 format change" (everything from Set-Cookie) from
    # "deploy regression" (mixed sources).
    logger.info(
        "qr_poll success cookies captured: "
        "sessdata={sess_src}, bili_jct={jct_src}, dede_user_id={dede_src}",
        sess_src="Set-Cookie" if _cookie_value(response, "SESSDATA") else "data.url",
        jct_src="Set-Cookie" if _cookie_value(response, "bili_jct") else "data.url",
        dede_src="Set-Cookie" if _cookie_value(response, "DedeUserID") else "data.url",
    )

    # Masked-value diagnostic: confirms the captured cookies are real
    # non-empty strings (rather than the historical "" empty case that
    # drove the EXPIRED-state bug). First 6 chars + length — never the
    # full secret.
    def _mask(value: str) -> str:
        if not value:
            return "<empty>"
        return f"{value[:6]}... (len={len(value)})"

    logger.info(
        "captured sessdata={} bili_jct={} dede_user_id={}",
        _mask(sessdata),
        _mask(bili_jct),
        _mask(dede_user_id),
    )

    return {
        "sessdata": sessdata,
        "bili_jct": bili_jct,
        "dede_user_id": dede_user_id,
    }


# ---------------------------------------------------------------------------
# QR flow — generate + poll
# ---------------------------------------------------------------------------


async def qr_generate(client: httpx.AsyncClient) -> dict[str, str]:
    """GET ``/x/passport-login/web/qrcode/generate`` and return ``{qrcode_url, qrcode_key}``.

    Raises:
        QrLoginError: on non-zero code, missing ``data`` field, or a
            malformed body. Callers should treat any raise as "QR flow
            cannot start" — usually a transient network issue.
    """
    response = await client.get(_QR_GENERATE_URL, headers=_QR_HEADERS)
    body = _parse_json_object(response.text)

    code = body.get("code")
    if code != 0:
        raise QrLoginError(
            f"qr_generate: non-zero code={code!r} message={body.get('message')!r}"
        )

    data = body.get("data")
    if not isinstance(data, dict):
        raise QrLoginError("qr_generate: missing or non-object 'data'")

    # B站 renamed ``qrcode_url`` → ``url``; prefer the new name, keep the
    # legacy field as a fallback. Response key stays ``qrcode_url``.
    url_obj = data.get("url")
    legacy_url_obj = data.get("qrcode_url")
    raw_scan_link: object = url_obj if isinstance(url_obj, str) else legacy_url_obj
    qrcode_key_obj = data.get("qrcode_key")
    if not isinstance(raw_scan_link, str) or not isinstance(qrcode_key_obj, str):
        raise QrLoginError("qr_generate: data missing url / qrcode_key")
    if not raw_scan_link or not qrcode_key_obj:
        raise QrLoginError("qr_generate: data has empty url / qrcode_key")
    qrcode_url: str = raw_scan_link
    qrcode_key: str = qrcode_key_obj

    return {"qrcode_url": qrcode_url, "qrcode_key": qrcode_key}


async def qr_poll(client: httpx.AsyncClient, qrcode_key: str) -> dict[str, str]:
    """GET ``/x/passport-login/web/qrcode/poll?qrcode_key=...`` and dispatch by code.

    Important: B站's poll response uses TWO separate code fields and the
    top-level envelope ``code`` is ALWAYS ``0`` on every poll (success
    OR an intermediate state) — the QR-login state code is in
    ``data.code`` (86101 / 86090 / 86038 / 0). Reading the top-level
    ``code`` would incorrectly treat every poll as successful and mask
    intermediate states.

    Returns:
        A success dict ``{status, sessdata, bili_jct, dede_user_id}`` on
        B站 inner code 0.

    Raises:
        QrExpiredError:        inner code 86038 — the QR expired, regenerate.
        QrAwaitingScanError:   inner code 86101 — user has not scanned yet.
        QrAwaitingConfirmError: inner code 86090 — scanned, awaiting confirm.
        LoginIncompleteError:   inner code 0 but SESSDATA / bili_jct missing
                                after BOTH Set-Cookie and ``data.url`` were
                                consulted.
        QrLoginError:           any other inner code, or a malformed body.
    """
    response = await client.get(
        _QR_POLL_URL,
        params={"qrcode_key": qrcode_key},
        headers=_QR_HEADERS,
    )
    body = _parse_json_object(response.text)

    # B站 nests the QR state code in ``data.code`` (NOT the top-level
    # ``code`` — that is always 0). Pull it out, accepting either an int
    # or a numeric string form (some B站 deployments serialise it as a
    # string for historic reasons).
    data = body.get("data")
    if not isinstance(data, dict):
        raise QrLoginError(
            f"qr_poll: missing or non-object 'data' (raw code={body.get('code')!r})"
        )
    raw_inner: object = data.get("code")
    if isinstance(raw_inner, int):
        inner_code: int | None = raw_inner
    elif isinstance(raw_inner, str) and raw_inner.lstrip("-").isdigit():
        inner_code = int(raw_inner)
    else:
        inner_code = None

    # Diagnostics: log every poll's inner code so operators can see the
    # full 86101→86090→0 transition in production logs.
    inner_msg: object = data.get("message")
    logger.info(
        "qr_poll: inner_code={inner_code} message={inner_message!r}",
        inner_code=inner_code,
        inner_message=inner_msg if isinstance(inner_msg, str) else "",
    )

    if inner_code == 86101:
        raise QrAwaitingScanError()
    if inner_code == 86090:
        raise QrAwaitingConfirmError()
    if inner_code == 86038:
        raise QrExpiredError()
    if inner_code == 0:
        # success path — capture cookies from BOTH Set-Cookie and
        # ``data.url`` (Set-Cookie is primary; ``data.url`` is the legacy
        # fallback for any cookie still missing).
        cookies = _capture_success_cookies(response)

        if not cookies["bili_jct"]:
            raise LoginIncompleteError(
                "qr_poll: success but bili_jct missing from BOTH url and Set-Cookie"
            )
        if not cookies["sessdata"]:
            raise LoginIncompleteError("qr_poll: success but SESSDATA missing")

        return {
            "status": "success",
            "sessdata": cookies["sessdata"],
            "bili_jct": cookies["bili_jct"],
            "dede_user_id": cookies["dede_user_id"],
        }

    # Unknown / missing inner code — surface it so we can see a B站 format
    # change in production logs rather than silently succeeding.
    raise QrLoginError(
        f"qr_poll: unknown inner_code={inner_code!r} (message={inner_msg!r})"
    )


# ---------------------------------------------------------------------------
# Device fingerprint (buvid3) — captured from /x/frontend/finger/spi
# ---------------------------------------------------------------------------


async def fetch_buvid3(client: httpx.AsyncClient) -> str | None:
    """GET ``/x/frontend/finger/spi`` and return the ``data.b_3`` value.

    B站's QR-login flow sets SESSDATA / bili_jct / DedeUserID via Set-Cookie
    but does NOT set ``buvid3`` (the device fingerprint). ``buvid3`` is
    required by some WBI-signed endpoints (e.g.
    ``/xlive/web-room/v1/index/getDanmuInfo``) and is normally generated
    by the B站 web client on its first page load via the public
    ``/x/frontend/finger/spi`` endpoint. We replay that call here right
    after a successful QR poll so the freshly-persisted ``.env`` carries
    a usable buvid3 for downstream WBI calls.

    Best-effort: returns ``None`` on any failure (non-zero code, missing
    ``data.b_3``, HTTP error, malformed body). The caller (auth_routes)
    threads the result into ``write_env_atomic`` and
    ``mark_authenticated_after_login`` — ``None`` is acceptable because
    the SESSDATA + bili_jct pair is already sufficient to authenticate
    the user; buvid3 is a nice-to-have for the danmaku WebSocket token
    fetch.

    Args:
        client: shared ``httpx.AsyncClient``. The same UA / Referer as
            the QR flow are fine — /spi is a public endpoint that does
            not require authentication.

    Returns:
        The ``b_3`` value (a non-empty string), or ``None`` on any
        failure path. Never raises.
    """
    try:
        response = await client.get(_BUVID3_SPI_URL, headers=_QR_HEADERS)
    except httpx.HTTPError as exc:
        logger.warning(
            "fetch_buvid3: /spi request failed: {}", exc
        )
        return None

    if response.status_code != 200:
        logger.warning(
            "fetch_buvid3: /spi returned status={}", response.status_code
        )
        return None

    body = _parse_json_object(response.text)
    if body.get("code") != 0:
        logger.warning(
            "fetch_buvid3: /spi non-zero code={} message={}",
            body.get("code"),
            body.get("message"),
        )
        return None

    data = body.get("data")
    if not isinstance(data, dict):
        return None
    b3_obj = data.get("b_3")
    if not isinstance(b3_obj, str) or not b3_obj:
        return None
    return b3_obj


# ---------------------------------------------------------------------------
# Atomic .env writer
# ---------------------------------------------------------------------------


def _read_existing_lines(env_path: Path) -> list[str]:
    """Read the .env lines verbatim, preserving ordering and comments.

    Missing file → ``[]``. Decoding errors → ``[]`` (we'd rather write a
    fresh file than crash on a corrupt one).
    """
    if not env_path.exists():
        return []
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return []
    # Strip a single trailing newline so our re-emit is deterministic;
    # we re-add one when joining.
    return text.splitlines()


def _update_owned_lines(lines: list[str], updates: dict[str, str]) -> list[str]:
    """Return a new line list with ``updates`` applied IN-PLACE.

    Rules:
      - For every key present in ``lines``, the FIRST occurrence is
        rewritten to the new value. Subsequent occurrences (duplicates)
        are dropped.
      - Keys missing from ``lines`` are appended at the end in the order
        they appear in ``updates``.
      - Lines not matching an owned key are preserved verbatim.
      - Blank lines and comment lines are preserved verbatim.
    """
    owned = set(_ENV_KEYS_OWNED)
    seen: set[str] = set()
    out: list[str] = []

    for line in lines:
        stripped = line.strip()
        # Match ``KEY=rest`` with KEY consisting of A-Z / 0-9 / underscore.
        key: str = ""
        if "=" in stripped and not stripped.startswith("#"):
            head = stripped.split("=", 1)[0].strip()
            if head and all(c.isalnum() or c == "_" for c in head) and head in owned:
                key = head
        if key and key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        elif key:
            # Owned key we're NOT updating this call (defensive; updates
            # should contain all owned keys we want to write, but be
            # tolerant of partial updates). Keep the existing line as-is.
            out.append(line)
            seen.add(key)
        else:
            out.append(line)

    # Append any owned keys that weren't already present.
    for key in _ENV_KEYS_OWNED:
        if key in updates and key not in seen:
            out.append(f"{key}={updates[key]}")

    return out


def write_env_atomic(
    sessdata: str,
    bili_jct: str,
    buvid3: str | None,
    env_path: Path,
) -> None:
    """Persist the three cookies to ``env_path`` atomically.

    Behaviour:
      - Reads ``env_path`` if it exists; preserves every other line.
      - Replaces the first ``SESSDATA=`` / ``BILI_JCT=`` / ``BUVID3=``
        line with the new value. Duplicates of these keys are dropped.
      - Appends any missing owned key at the end.
      - ``buvid3=None`` → the ``BUVID3=`` line is NOT touched (we don't
        add an empty placeholder, and we don't clear an existing value).
      - Writes to ``env_path.with_suffix('.env.tmp')`` first, then
        ``os.replace``s it into place. A crash mid-write can never leave
        a half-written ``.env`` visible to readers.
    """
    updates: dict[str, str] = {
        "SESSDATA": sessdata,
        "BILI_JCT": bili_jct,
    }
    if buvid3 is not None:
        updates["BUVID3"] = buvid3

    existing_lines = _read_existing_lines(env_path)
    new_lines = _update_owned_lines(existing_lines, updates)
    new_text = "\n".join(new_lines)
    # Always end with a single trailing newline so editors / shells are
    # happy reading the file.
    if new_text and not new_text.endswith("\n"):
        new_text += "\n"

    tmp_path = env_path.with_name(env_path.name + _ENV_TMP_SUFFIX)
    # Write to staging file, fsync, then atomically rename. ``os.replace``
    # is atomic on POSIX when src and dst are on the same filesystem
    # (which they are — same directory).
    with tmp_path.open("w", encoding="utf-8") as fh:
        fh.write(new_text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, env_path)


# ---------------------------------------------------------------------------
# Plan B — manual cookie entry
# ---------------------------------------------------------------------------


async def save_cookies_manual(
    sessdata: str,
    bili_jct: str,
    buvid3: str | None,
    env_path: Path,
) -> dict[str, object]:
    """Validate user-provided cookies via ``/nav`` and persist on success.

    The validation step uses a one-shot :class:`BilibiliClient` that
    OWNS its own ``httpx.AsyncClient`` (we deliberately do NOT inject
    the shared request-scoped client). Constructing without an injected
    client bakes the cookies into the httpx client's cookie jar so the
    ``/nav`` call actually carries them — an injected client would not
    get its jar updated by the constructor, which silently produced
    ``nav -101`` on every manual login attempt.

    :meth:`BilibiliClient.get_user_info` returns ``None`` when ``nav``
    reports a non-zero business code (including ``-101`` not-logged-in),
    which we treat as "invalid cookies". On a non-empty data dict we
    extract ``uname`` / ``mid`` and atomically persist.

    Failure semantics: on any validation failure we raise
    :class:`LoginIncompleteError` and do NOT touch ``env_path``. The
    caller's existing ``.env`` (if any) is preserved verbatim.
    """
    # Local import to avoid a circular dependency (BilibiliClient imports
    # from app.bilibili.exceptions; auth.py sits alongside it but lives
    # upstream of FastAPI wiring — keep the import surface narrow).
    from app.bilibili.client import BilibiliClient

    cookies: dict[str, str] = {
        "SESSDATA": sessdata,
        "bili_jct": bili_jct,
    }
    if buvid3 is not None:
        cookies["buvid3"] = buvid3

    # No ``client=`` injection — BilibiliClient builds its own httpx
    # client with the cookies in the jar at construction time.
    bili_client = BilibiliClient(
        cookies=cookies,
        csrf_token=bili_jct,
    )
    try:
        data = await bili_client.get_user_info()
    finally:
        await bili_client.close()

    if not isinstance(data, dict):
        raise LoginIncompleteError(
            "save_cookies_manual: /nav did not return a valid user record"
        )

    uname = data.get("uname")
    mid = data.get("mid")
    if not isinstance(uname, str) or not isinstance(mid, int):
        raise LoginIncompleteError(
            "save_cookies_manual: /nav response missing uname / mid"
        )

    # Validation succeeded → persist atomically.
    write_env_atomic(
        sessdata=sessdata,
        bili_jct=bili_jct,
        buvid3=buvid3,
        env_path=env_path,
    )

    return {"uname": uname, "mid": mid}


__all__ = [
    "LoginIncompleteError",
    "QrAwaitingConfirmError",
    "QrAwaitingScanError",
    "QrExpiredError",
    "QrLoginError",
    "fetch_buvid3",
    "qr_generate",
    "qr_poll",
    "save_cookies_manual",
    "write_env_atomic",
]
