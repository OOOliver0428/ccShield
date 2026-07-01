"""Authentication state machine + cookie-expiry callback registry.

This module owns the runtime representation of "is the user logged in, and
if not, why?". Three states cover every case the rest of the application
needs to discriminate:

    AUTHENTICATED  — /nav returned a user record; cookies are fresh.
    NEEDS_LOGIN    — no cookies configured (cold start) OR cookies were
                     never verified. Caller should run the QR flow.
    EXPIRED        — cookies were present but /nav rejected them (or any
                     mid-session API call raised AuthExpiredError).
                     Caller should trigger cookie-refresh and prompt the
                     user.

Why a state machine and not a boolean:
- ccShield's main.py only WARNED on missing cookies. The user had to notice
  the warning. We BLOCK with ``require_authenticated()`` so the FastAPI
  routes cannot accidentally serve moderator actions with a stale
  session.
- Mid-session expiry has to be observable to the WS push layer (so it
  can stop sending danmaku) and to the cookie-refresh coordinator. We
  expose that surface via ``on_expired(callback)`` + ``handle_auth_expired()``.

Design constraints:
- Constructor injection for ``bili_client``, ``sessdata``, ``bili_jct``.
  Production wires these through the module-level ``auth_session``
  singleton; tests pass literals and a mock ``bili_client``.
- A missing or partial ``.env`` MUST NOT crash startup. ``check_on_startup``
  defaults to ``NEEDS_LOGIN`` when settings cannot be imported at all.
- ``handle_auth_expired`` swallows callback exceptions so a single bad
  listener cannot poison the others.
- All callbacks are async (``Callable[[], Awaitable[None]]``). A sync
  callable would silently produce a non-awaitable on the next dispatch.
- No use of ``Any``. Every public attribute / parameter carries an
  explicit type.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import Enum

from loguru import logger

from app.bilibili.exceptions import AuthExpiredError

# ``BilibiliClient`` is imported lazily inside ``_build_singleton`` to
# avoid coupling this module's import path to ``app.bilibili.client``
# (defensive against future import-order surprises).


class AuthState(Enum):
    """Tri-state authentication flag."""

    AUTHENTICATED = "authenticated"
    NEEDS_LOGIN = "needs_login"
    EXPIRED = "expired"


class NotAuthenticatedError(Exception):
    """Raised by :meth:`AuthSession.require_authenticated` when the session
    is not in :attr:`AuthState.AUTHENTICATED`.

    Routes catch this to surface a 401/403 response with a useful body.
    """


# ---------------------------------------------------------------------------
# Settings lookup (lazy + defensive)
# ---------------------------------------------------------------------------


def _load_cookie_settings() -> tuple[str, str]:
    """Return ``(SESSDATA, BILI_JCT)`` from :mod:`app.config`, or ``("", "")``.

    We deliberately do NOT raise on a missing / unimportable config — a
    cold start with no ``.env`` is the most common state and must surface
    as ``NEEDS_LOGIN``, not a 500.
    """
    try:
        from app.config import settings  # lazy: T2 owns config.py
    except ImportError:
        return "", ""
    try:
        sessdata = str(getattr(settings, "SESSDATA", "") or "")
    except AttributeError:
        sessdata = ""
    try:
        bili_jct = str(getattr(settings, "BILI_JCT", "") or "")
    except AttributeError:
        bili_jct = ""
    return sessdata, bili_jct


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


# A registered callback is async-no-arg-no-return. Sync callables would
# silently produce non-awaitables on dispatch; we accept the type at the
# boundary so misuse is caught at registration time by static analysis.
OnExpiredCallback = Callable[[], Awaitable[None]]


class AuthSession:
    """Process-wide authentication state machine.

    Args:
        bili_client: the typed B站 HTTP client (``BilibiliClient`` or any
            object exposing an ``async get_user_info()`` method that
            returns a dict on success, ``None`` on ``-101``).
        sessdata: optional override for the SESSDATA cookie. When ``None``
            we read from :mod:`app.config.settings` on every call.
            Constructor injection lets tests pass literal strings without
            monkeypatching the global settings singleton.
        bili_jct: optional override for the bili_jct cookie. Same semantics
            as ``sessdata``.
    """

    def __init__(
        self,
        bili_client: object,
        sessdata: str | None = None,
        bili_jct: str | None = None,
    ) -> None:
        # ``bili_client`` is typed as ``object`` rather than ``BilibiliClient``
        # to avoid an import cycle at type-check time (the test mocks are
        # AsyncMock instances). At runtime we only call ``.get_user_info()``
        # which is duck-typed via the BilibiliClient protocol.
        self._bili_client: object = bili_client
        self._sessdata_override: str | None = sessdata
        self._bili_jct_override: str | None = bili_jct
        self._state: AuthState = AuthState.NEEDS_LOGIN
        self._on_expired_callbacks: list[OnExpiredCallback] = []

    # ------------------------------------------------------------------ #
    # State property
    # ------------------------------------------------------------------ #

    @property
    def state(self) -> AuthState:
        """The current auth state. Read-only for callers."""
        return self._state

    # ------------------------------------------------------------------ #
    # Startup check
    # ------------------------------------------------------------------ #

    async def check_on_startup(self) -> AuthState:
        """Verify stored cookies by calling ``/x/web-interface/nav``.

        Returns the new state:

        - ``NEEDS_LOGIN`` when SESSDATA or BILI_JCT is empty. ``/nav`` is
          NOT called in this branch — saving a wasted round-trip on every
          cold start.
        - ``AUTHENTICATED`` when ``/nav`` returns a dict (cookies valid).
        - ``EXPIRED`` when ``/nav`` returns ``None`` OR raises
          :class:`AuthExpiredError` (cookies stale).

        Network errors that aren't :class:`AuthExpiredError` propagate;
        they're a real connection problem the caller needs to see.
        """
        sessdata, bili_jct = self._resolve_cookies()

        if not sessdata or not bili_jct:
            self._state = AuthState.NEEDS_LOGIN
            return self._state

        try:
            data = await self._bili_client.get_user_info()  # type: ignore[attr-defined]
        except AuthExpiredError:
            logger.warning(
                "AuthSession: /nav raised AuthExpiredError; cookies expired"
            )
            self._state = AuthState.EXPIRED
            return self._state

        if isinstance(data, dict) and data:
            self._state = AuthState.AUTHENTICATED
            return self._state

        logger.warning("AuthSession: /nav returned None; cookies expired")
        self._state = AuthState.EXPIRED
        return self._state

    async def mark_authenticated_after_login(
        self,
        sessdata: str,
        bili_jct: str,
        buvid3: str | None = None,
    ) -> AuthState:
        """Hot-reload the auth state after a successful QR or manual login.

        Why this exists:
            :data:`app.config.settings` is constructed at module-import
            time. After we :func:`write_env_atomic` the freshly-acquired
            cookies to disk, the in-memory ``settings`` still carries the
            OLD (empty) cookie values. Re-firing :meth:`check_on_startup`
            would therefore keep reading stale cookies, see them as
            empty, and leave the state machine in ``NEEDS_LOGIN`` — the
            QR / manual flow would appear successful yet
            ``GET /auth/status`` still reports ``needs_login``.

        What it does:
            1. Writes the new ``SESSDATA`` / ``BILI_JCT`` (and optional
               ``BUVID3``) onto the live :data:`app.config.settings`
               singleton in-memory — same module the next
               :meth:`check_on_startup` will read from via
               :func:`_load_cookie_settings`.
            2. Calls :meth:`check_on_startup` once. Because step 1 has
               already populated the in-memory cookies, ``/nav`` is
               actually called this time; cookies were just validated
               by the B站 API on the QR / manual path, so we expect
               ``AUTHENTICATED``.

        Args:
            sessdata: the cookie just persisted to ``.env``.
            bili_jct: the csrf cookie just persisted to ``.env``.
            buvid3: optional device fingerprint cookie. ``None`` leaves
                the existing in-memory ``BUVID3`` value untouched (we
                do not want to clear a previously-set BUVID3 with an
                empty placeholder).

        Returns:
            The new auth state — ``AUTHENTICATED`` on the happy path,
            ``EXPIRED`` if the cookies turned out stale (should never
            happen because the QR / manual flow already validated them).
        """
        # Lazy import: keeps ``app.auth.session`` importable on cold
        # paths where ``app.config`` is not yet present (mirrors the
        # defensive style of :func:`_load_cookie_settings`).
        from app.config import settings

        settings.SESSDATA = sessdata
        settings.BILI_JCT = bili_jct
        if buvid3 is not None:
            settings.BUVID3 = buvid3

        # Refresh the long-lived ``bili_client``'s httpx cookie jar BEFORE
        # firing ``/nav``. The singleton was constructed at module-import
        # time when ``.env`` was empty, so its cookie jar is empty until
        # we push the freshly-captured cookies in. Without this refresh
        # ``/nav`` still sees an empty jar, returns ``-101``, and the
        # state machine lands in EXPIRED even though the user just
        # successfully logged in. See a.log for the runtime-confirmed
        # symptom.
        update_cookies = getattr(self._bili_client, "update_cookies", None)
        if callable(update_cookies):
            update_cookies(dict(settings.cookies))

        return await self.check_on_startup()

    def _resolve_cookies(self) -> tuple[str, str]:
        """Pick the override values if provided; otherwise read settings."""
        if self._sessdata_override is not None and self._bili_jct_override is not None:
            return self._sessdata_override, self._bili_jct_override
        settings_sess, settings_jct = _load_cookie_settings()
        sessdata = (
            self._sessdata_override if self._sessdata_override is not None else settings_sess
        )
        bili_jct = (
            self._bili_jct_override if self._bili_jct_override is not None else settings_jct
        )
        return sessdata, bili_jct

    # ------------------------------------------------------------------ #
    # Callback registry
    # ------------------------------------------------------------------ #

    def on_expired(self, callback: OnExpiredCallback) -> None:
        """Register an async callback to fire on every EXPIRED transition.

        Callbacks fire in registration order. A raising callback does NOT
        prevent later ones from running — see :meth:`handle_auth_expired`.
        """
        self._on_expired_callbacks.append(callback)

    # ------------------------------------------------------------------ #
    # Mid-session expiry — invoked by C3 when any B站 API raises
    # AuthExpiredError mid-request.
    # ------------------------------------------------------------------ #

    async def handle_auth_expired(self) -> None:
        """Transition to EXPIRED and dispatch all registered callbacks.

        Each callback is awaited individually; an exception in one is
        logged and swallowed so the others still run. Idempotent: a second
        call from a duplicate hook re-fires every callback — by design,
        so freshly-added listeners learn about the existing outage.
        """
        self._state = AuthState.EXPIRED
        # Iterate over a snapshot so a callback that mutates the list
        # (e.g. re-registers itself) doesn't perturb this loop.
        for callback in list(self._on_expired_callbacks):
            try:
                await callback()
            except Exception:
                logger.exception(
                    "AuthSession: on_expired callback raised; continuing"
                )

    # ------------------------------------------------------------------ #
    # Route gating
    # ------------------------------------------------------------------ #

    def require_authenticated(self) -> None:
        """Raise :class:`NotAuthenticatedError` unless the session is AUTHENTICATED.

        Routes call this at the top of every handler that needs a valid
        session. The exception message carries the current state so
        401/403 responses can be debugged from the server log alone.
        """
        if self._state != AuthState.AUTHENTICATED:
            raise NotAuthenticatedError(
                f"authentication required (current state: {self._state.value})"
            )

    async def get_current_user(self) -> dict[str, object] | None:
        """Fetch the live user record from ``/nav`` via the shared Bili client.

        Returns:
            The ``data`` payload on code 0, else ``None``. Callers should
            treat ``None`` as "no user — return 401" rather than 500.

        Used by the ``/api/auth/me`` endpoint to surface ``uname`` / ``mid``
        to the frontend after QR / manual login.
        """
        get_user_info = getattr(self._bili_client, "get_user_info", None)
        if get_user_info is None:
            return None
        result: object = await get_user_info()
        return result if isinstance(result, dict) else None


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


def _build_singleton() -> AuthSession:
    """Lazy-import ``BilibiliClient`` and construct the process-wide session.

    Keeping the import inside a function (rather than at module top) means
    :mod:`app.auth.session` stays importable while the Bili client module
    is mid-flight during parallel T2/T4 work. We construct the singleton
    immediately so ``from app.auth.session import auth_session`` yields a
    ready-to-use object — but the underlying ``httpx.AsyncClient`` is not
    connected until first request, so this is safe.
    """
    from app.bilibili.client import BilibiliClient

    return AuthSession(BilibiliClient())


auth_session: AuthSession = _build_singleton()


__all__ = [
    "AuthSession",
    "AuthState",
    "NotAuthenticatedError",
    "auth_session",
]
