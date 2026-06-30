"""LOCAL_TOKEN + Host-guard middleware for the local FastAPI app.

The middleware enforces two policies on every request that reaches a
protected path:

1. **Host guard** — the request's Host header (with the port stripped) must
   be ``"localhost"`` or ``"127.0.0.1"``. This blocks DNS-rebinding attacks
   and the "expose your local token via a hostile Host header" footgun.
   It runs BEFORE the token check so a malicious host cannot probe the
   token-validity oracle.

2. **LOCAL_TOKEN** — the request must carry ``Authorization: Bearer <token>``
   where ``<token>`` equals ``settings.LOCAL_TOKEN`` (a process-lifetime
   128-bit hex secret generated lazily on first read; see
   :mod:`app.config`). A missing or wrong token → 401.

Exempt paths (no guard, no token needed):

- ``/health`` — liveness probe; orchestrators may not be able to set the
  Host or Authorization header.
- ``/openapi.json`` and ``/docs*`` — OpenAPI docs are developer-facing
  and must work without auth so a fresh install can be inspected.

WebSocket exemptions:

- WebSocket endpoints cannot set custom headers reliably from the browser
  (the browser WebSocket API exposes headers only on the same-origin
  server, and only some clients honour ``Authorization``). For paths
  matching ``/ws/*`` OR ``/api/ws/*`` the middleware accepts
  ``?token=<settings.LOCAL_TOKEN>`` as a query-string fallback. The Host
  guard still applies. The ``/api/ws/*`` branch covers T13's room-WS
  endpoint, which is mounted under the ``/api`` APIRouter (so its real
  URL is ``/api/ws/rooms/{room_id}``); the original ``/ws/*`` branch
  remains for any future bare-mounted WS routes.

Anti-pattern reference:
    ccShield routes.py:120-175 implemented a multi-user Bearer-token
    scheme (one token per user, plumbed through every handler). We do
    LOCAL_TOKEN + localhost bypass instead — the local-only tool has
    exactly one trust boundary, the developer's machine, and one
    process-lifetime secret.

Strict typing: every parameter and return value carries an explicit type.
``Any`` is not used.
"""
from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

# Paths that bypass BOTH the host guard and the token gate. Prefix-match
# so /docs and /docs/oauth2-redirect are both exempt.
_FULLY_EXEMPT_PATH_PREFIXES: tuple[str, ...] = (
    "/health",
    "/openapi",
    "/docs",
    "/redoc",
)

# Paths that bypass only the LOCAL_TOKEN gate (chicken-and-egg endpoints
# the SPA must call before it knows the token). The Host guard further
# down STILL runs on them, so only a request from ``localhost`` /
# ``127.0.0.1`` can succeed. ``/api/auth/bootstrap`` is the only member
# today; new entries MUST come with a written rationale.
#
# See ``auth_routes.auth_bootstrap`` for the full trust-model rationale.
_TOKEN_EXEMPT_PATH_PREFIXES: tuple[str, ...] = (
    "/api/auth/bootstrap",
)
# Path prefixes that REQUIRE auth + host guard.
_PROTECTED_PATH_PREFIXES: tuple[str, ...] = (
    "/api",
    "/ws",
)
# Hosts accepted on protected paths. We compare the hostname (port
# stripped) against this tuple. A 2-element tuple is fine here:
# ``in`` lookup is O(2) and the list is effectively immutable.
_ALLOWED_HOSTS: tuple[str, ...] = ("localhost", "127.0.0.1")

# Type alias for the next-handler callback in a Starlette middleware.
DispatchCallable = Callable[[Request], Awaitable[Response]]


def _is_fully_exempt(path: str) -> bool:
    """Return True when ``path`` is exempt from BOTH host guard and token gate.

    Both exact matches and prefix matches are accepted so a future
    ``/health/deep`` would also be exempt.
    """
    return any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in _FULLY_EXEMPT_PATH_PREFIXES
    )


def _is_token_exempt(path: str) -> bool:
    """Return True when ``path`` is exempt only from the LOCAL_TOKEN gate.

    The host guard still runs on these paths — that is the whole point of
    having a separate list: chicken-and-egg endpoints the SPA must call
    before it knows the token, but which must remain unreachable from
    external origins.
    """
    return any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in _TOKEN_EXEMPT_PATH_PREFIXES
    )


def _is_protected(path: str) -> bool:
    """Return True when ``path`` is under a protected prefix (``/api/*``, ``/ws/*``)."""
    return any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in _PROTECTED_PATH_PREFIXES
    )


def _extract_bearer(authorization_header: str | None) -> str | None:
    """Parse ``Authorization: Bearer <token>``; return the token or ``None``.

    The comparison is case-sensitive on the scheme name (``Bearer``) — that
    is what RFC 6750 §2.1 mandates.
    """
    if not authorization_header:
        return None
    parts = authorization_header.split(" ", 1)
    if len(parts) != 2:
        return None
    scheme, token = parts
    if scheme != "Bearer":
        return None
    return token


def _host_is_allowed(request: Request) -> bool:
    """Return True iff the request's Host header hostname is in the allow-list.

    Uses :func:`urllib.parse.urlsplit` so any port suffix is stripped
    before comparison — ``localhost:8000`` is the same host as
    ``localhost``. An unparseable / missing Host header is rejected.
    """
    raw_host: str | None = request.headers.get("host")
    if not raw_host:
        return False
    parsed = urlsplit("//" + raw_host)
    hostname: str = parsed.hostname or ""
    return hostname in _ALLOWED_HOSTS


def _token_matches(supplied: str, expected: str) -> bool:
    """Constant-time string comparison via :func:`secrets.compare_digest`.

    Both strings are length-checked first; ``compare_digest`` requires
    equal-length inputs to be timing-safe. A length mismatch is a fast
    reject (the lengths are not secret; the secret is the value).
    """
    if len(supplied) != len(expected):
        return False
    return secrets.compare_digest(supplied, expected)


def _is_ws_path(path: str) -> bool:
    """Return True for paths that may carry a WebSocket credential via query string.

    The browser ``WebSocket`` API cannot reliably set the ``Authorization``
    header, so WS endpoints must accept ``?token=<token>`` instead. Both
    the bare ``/ws/*`` mount and the ``/api/ws/*`` mount (T13's room
    bridge, mounted under :data:`app.api.api_router` with prefix
    ``/api``) are recognized — the query-string fallback is the only
    auth shape browser-side WS clients can use.
    """
    return path.startswith("/ws") or path.startswith("/api/ws")


def _request_carries_valid_token(request: Request) -> bool:
    """Return True iff the request carries a valid LOCAL_TOKEN credential.

    Two accepted shapes:

    - HTTP/REST: ``Authorization: Bearer <token>`` header.
    - WebSocket (``/ws/*`` or ``/api/ws/*``): ``?token=<token>`` query
      parameter, because browser WebSocket clients cannot reliably set
      custom headers.
    """
    expected: str = settings.LOCAL_TOKEN
    bearer: str | None = _extract_bearer(request.headers.get("authorization"))
    if bearer is not None:
        return _token_matches(bearer, expected)
    if _is_ws_path(request.url.path):
        query_token: str = request.query_params.get("token", "")
        if not query_token:
            return False
        return _token_matches(query_token, expected)
    return False


class LocalTokenMiddleware(BaseHTTPMiddleware):
    """ASGI middleware enforcing host + LOCAL_TOKEN guard on protected paths.

    Inherits from :class:`starlette.middleware.base.BaseHTTPMiddleware`
    so the existing Starlette ``request.scope`` is wrapped into a
    :class:`fastapi.Request` and ``call_next`` returns a :class:`Response`.
    This is the path FastAPI itself uses for its built-in middlewares.

    FastAPI's :meth:`add_middleware` instantiates the class with the
    surrounding ASGI app as its first argument, exactly as
    :class:`BaseHTTPMiddleware` requires.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: DispatchCallable,
    ) -> Response:
        """Inspect, gate, then forward to the next handler.

        Order matters:

        1. Fully-exempt paths short-circuit (no work, no logging) — covers
           ``/health``, ``/openapi``, ``/docs``, ``/redoc``.
        2. Unprotected paths skip the guard (e.g. future non-/api routes
           we haven't classified yet — fail-open here is intentional so
           a misconfigured prefix doesn't lock out the developer).
        3. Host guard runs FIRST on protected paths; the 403 response
           reveals nothing about token validity.
        4. Token check runs LAST, **except** for paths in
           :data:`_TOKEN_EXEMPT_PATH_PREFIXES` (currently just
           ``/api/auth/bootstrap``) which the SPA must hit before it
           knows the token.
        """
        path: str = request.url.path

        if _is_fully_exempt(path):
            return await call_next(request)

        if not _is_protected(path):
            # Future non-/api non-/ws routes fall through unguarded. T13
            # and later tasks add the prefixes; until then we deliberately
            # do not 403 — locking out the developer is worse than a
            # missing guard on a not-yet-existing path.
            return await call_next(request)

        if not _host_is_allowed(request):
            logger.warning(
                "LocalTokenMiddleware: rejected request with non-local host "
                "(host={!r}, path={!r})",
                request.headers.get("host"),
                path,
            )
            return JSONResponse(
                {"detail": "forbidden: host not in allow-list"},
                status_code=403,
            )

        if _is_token_exempt(path):
            return await call_next(request)

        if not _request_carries_valid_token(request):
            return JSONResponse(
                {"detail": "unauthorized: missing or invalid LOCAL_TOKEN"},
                status_code=401,
            )

        return await call_next(request)


__all__ = ["LocalTokenMiddleware"]
