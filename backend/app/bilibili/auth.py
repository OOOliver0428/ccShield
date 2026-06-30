"""B站 (Bilibili) QR-code login + manual-fallback cookie persistence.

This module owns the QR-code login flow that reccshield uses to obtain
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

3. On success (code 0), the auth cookies live in the ``data.url`` query
   params (B站 returns a redirect URL containing SESSDATA, bili_jct,
   DedeUserID). We capture ``bili_jct`` from the URL FIRST (primary path)
   and only fall back to ``response.cookies['bili_jct']`` (Set-Cookie
   header) when the URL does not carry it. If neither path yields a
   non-empty ``bili_jct`` AND ``SESSDATA``, ``LoginIncompleteError`` is
   raised — we never persist a partial credential set.

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
- No FastAPI / HTTP routes here — T8 owns the route layer. This module
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

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

_QR_GENERATE_URL: str = (
    "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
)
_QR_POLL_URL: str = (
    "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
)

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


def _first_query_value(
    parsed_url: object, params: object, key: str
) -> str:
    """Return the first ``params[key]`` entry if it is a non-empty string."""
    if not isinstance(params, dict):
        return ""
    raw: object = params.get(key)
    if not isinstance(raw, list) or not raw:
        return ""
    first: object = raw[0]
    return first if isinstance(first, str) else ""
    # parsed_url reserved for future use (kept for symmetry / debug)
    del parsed_url


def _capture_success_cookies(
    url: str, response_cookies: httpx.Response | httpx.AsyncClient | object,
) -> dict[str, str]:
    """Pull SESSDATA / bili_jct / DedeUserID from B站's success response.

    Strategy (dual-path):
      1. Parse ``url`` query params with ``urllib.parse.parse_qs``. The
         URL is the PRIMARY source — B站 builds it server-side and is
         more reliable than cookie parsing.
      2. If ``bili_jct`` is empty in the URL, fall back to the response's
         Set-Cookie (``response_cookies.get('bili_jct')``).
      3. SESSDATA and DedeUserID come from the URL ONLY — B站 does not
         set them in Set-Cookie on this endpoint.

    The third positional argument exists so this function is callable
    against both a raw ``httpx.Response`` and any object exposing a
    ``cookies`` jar; the public callers always pass ``response``.
    """
    parsed_url = urlparse(url)
    query_params = parse_qs(parsed_url.query)

    sessdata = _first_query_value(parsed_url, query_params, "SESSDATA")
    dede_user_id = _first_query_value(parsed_url, query_params, "DedeUserID")
    bili_jct = _first_query_value(parsed_url, query_params, "bili_jct")

    # Fallback: only used when the URL did not carry bili_jct.
    if not bili_jct:
        # ``response_cookies`` here is the httpx.Response itself; we look
        # up via its .cookies jar so the caller doesn't need to extract it.
        candidate: object = None
        if isinstance(response_cookies, httpx.Response):
            candidate = response_cookies.cookies.get("bili_jct")
        elif hasattr(response_cookies, "cookies"):
            jar: object = getattr(response_cookies, "cookies", None)
            if jar is not None and hasattr(jar, "get"):
                candidate = jar.get("bili_jct")  # type: ignore[attr-defined]
        bili_jct = candidate if isinstance(candidate, str) else ""

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

    qrcode_url = data.get("qrcode_url")
    qrcode_key = data.get("qrcode_key")
    if not isinstance(qrcode_url, str) or not isinstance(qrcode_key, str):
        raise QrLoginError("qr_generate: data missing qrcode_url / qrcode_key")
    if not qrcode_url or not qrcode_key:
        raise QrLoginError("qr_generate: data has empty qrcode_url / qrcode_key")

    return {"qrcode_url": qrcode_url, "qrcode_key": qrcode_key}


async def qr_poll(client: httpx.AsyncClient, qrcode_key: str) -> dict[str, str]:
    """GET ``/x/passport-login/web/qrcode/poll?qrcode_key=...`` and dispatch by code.

    Returns:
        A success dict ``{status, sessdata, bili_jct, dede_user_id}`` on
        B站 code 0.

    Raises:
        QrExpiredError:        code 86038 — the QR expired, regenerate.
        QrAwaitingScanError:   code 86101 — user has not scanned yet.
        QrAwaitingConfirmError: code 86090 — scanned, awaiting phone confirm.
        LoginIncompleteError:   code 0 but SESSDATA / bili_jct missing.
        QrLoginError:           any other non-zero code, or a malformed body.
    """
    response = await client.get(
        _QR_POLL_URL,
        params={"qrcode_key": qrcode_key},
        headers=_QR_HEADERS,
    )
    body = _parse_json_object(response.text)
    code = body.get("code")

    if code == 86101 or code == "86101":
        raise QrAwaitingScanError()
    if code == 86090 or code == "86090":
        raise QrAwaitingConfirmError()
    if code == 86038 or code == "86038":
        raise QrExpiredError()

    if code != 0 and code != "0":
        raise QrLoginError(
            f"qr_poll: unexpected code={code!r} message={body.get('message')!r}"
        )

    # code == 0: success — capture cookies.
    data = body.get("data")
    if not isinstance(data, dict):
        raise LoginIncompleteError("qr_poll: success but data missing/non-object")

    url = data.get("url")
    if not isinstance(url, str) or not url:
        raise LoginIncompleteError("qr_poll: success but data.url missing")

    cookies = _capture_success_cookies(url, response)

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
    client: httpx.AsyncClient,
    sessdata: str,
    bili_jct: str,
    buvid3: str | None,
    env_path: Path,
) -> dict[str, object]:
    """Validate user-provided cookies via ``/nav`` and persist on success.

    The validation step uses a one-shot ``BilibiliClient`` configured with
    the provided cookies. ``get_user_info()`` returns ``None`` when ``nav``
    reports a non-zero business code (including ``-101`` not-logged-in),
    which we treat as "invalid cookies". On ``code == 0`` and a non-empty
    ``data`` dict we extract ``uname`` / ``mid`` and atomically persist.

    Failure semantics: on any validation failure we raise
    ``LoginIncompleteError`` and do NOT touch ``env_path``. The caller's
    existing ``.env`` (if any) is preserved verbatim.
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

    # Wire the cookies onto a BilibiliClient that uses the injected
    # ``client`` (which may carry a MockTransport in tests, or a real
    # httpx transport in production). The cookies get baked into the
    # client at construction time so the nav request carries them.
    bili_client = BilibiliClient(
        client=client,
        cookies=cookies,
        csrf_token=bili_jct,
    )
    # NOTE: we do NOT call ``bili_client.close()`` here — the injected
    # ``client`` is owned by the caller. Closing it would break any
    # further use the caller has planned (e.g. the FastAPI request
    # lifecycle). Tests build a fresh client per test so this is safe.
    data = await bili_client.get_user_info()

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

    # Validation succeeded → persist atomically. Note: we deliberately
    # do NOT write through ``client`` — the atomic write uses the
    # filesystem directly.
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
    "qr_generate",
    "qr_poll",
    "save_cookies_manual",
    "write_env_atomic",
]
