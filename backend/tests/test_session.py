"""TDD tests for AuthSession state machine (app.auth.session — T7).

- AuthState enum: AUTHENTICATED, NEEDS_LOGIN, EXPIRED.
- AuthSession.check_on_startup()
    * SESSDATA / BILI_JCT both empty → state == NEEDS_LOGIN; get_user_info NOT called.
    * cookies present + get_user_info returns a dict  → state == AUTHENTICATED.
    * cookies present + get_user_info returns None     → state == EXPIRED.
    * cookies present + get_user_info raises AuthExpiredError → state == EXPIRED,
      does NOT crash.
- AuthSession.handle_auth_expired() flips state to EXPIRED and awaits every
  registered on_expired callback. A raising callback does NOT prevent the
  others from running.
- AuthSession.require_authenticated() raises NotAuthenticatedError iff the
  state is not AUTHENTICATED.
- AuthSession.on_expired() accepts async callables (coroutine functions).

Mock strategy: constructor injection — AuthSession(bili_client, sessdata=..,
bili_jct=..) — so tests pass a mock bili_client plus literal cookie strings
without monkeypatching app.config.settings. Real settings are NOT read by
tests.

TDD step 1: tests FIRST. They MUST fail at collection (ModuleNotFoundError
on app.auth.session) and pass once the module lands.
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.auth.session import (
    AuthSession,
    AuthState,
    NotAuthenticatedError,
)
from app.bilibili.exceptions import AuthExpiredError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_mock_bili_client(get_user_info: Any | None = None) -> Any:
    """Build a minimal bili_client stand-in.

    Only ``get_user_info`` is required by AuthSession; the rest of the
    BilibiliClient surface is irrelevant for these tests. When
    ``get_user_info`` is omitted, the mock returns ``None`` by default
    (callers that need a different behaviour override ``cli.get_user_info``
    directly after construction).
    """
    cli = AsyncMock()
    if get_user_info is None:

        async def _default() -> dict[str, Any] | None:
            return None

        cli.get_user_info = _default
    else:
        cli.get_user_info = AsyncMock(side_effect=get_user_info)
    return cli


def run(coro):
    """Run an async coroutine to completion from a sync test."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Test 1 — happy path: cookies present + nav returns data → AUTHENTICATED.
# ---------------------------------------------------------------------------


def test_check_on_startup_authenticated_when_nav_returns_user_data() -> None:
    """SESSDATA+BILI_JCT present + /nav returns a user record → AUTHENTICATED."""
    bili = make_mock_bili_client(
        get_user_info=lambda: {"uname": "U", "mid": 1, "isLogin": True}
    )
    session = AuthSession(bili, sessdata="x", bili_jct="y")

    state = run(session.check_on_startup())

    assert state == AuthState.AUTHENTICATED
    assert session.state == AuthState.AUTHENTICATED
    bili.get_user_info.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 2 — empty cookies → NEEDS_LOGIN, get_user_info MUST NOT be called.
# ---------------------------------------------------------------------------


def test_check_on_startup_needs_login_when_cookies_empty() -> None:
    """Both SESSDATA and BILI_JCT empty → state == NEEDS_LOGIN.

    Crucially, we MUST NOT call /nav — there's no point hitting the API
    for an unconfigured client. This saves a wasted round-trip on every
    cold start.
    """
    bili = make_mock_bili_client(
        get_user_info=lambda: {"uname": "U", "mid": 1}  # never reached
    )
    session = AuthSession(bili, sessdata="", bili_jct="")

    state = run(session.check_on_startup())

    assert state == AuthState.NEEDS_LOGIN
    assert session.state == AuthState.NEEDS_LOGIN
    bili.get_user_info.assert_not_called()


def test_check_on_startup_needs_login_when_only_sessdata_empty() -> None:
    """SESSDATA empty (BILI_JCT alone is not enough) → NEEDS_LOGIN.

    Defensive: B站 needs BOTH cookies to be authenticated; a half-populated
    config is no better than an empty one.
    """
    bili = make_mock_bili_client(get_user_info=lambda: {"uname": "U"})
    session = AuthSession(bili, sessdata="", bili_jct="some_jct")

    state = run(session.check_on_startup())

    assert state == AuthState.NEEDS_LOGIN
    bili.get_user_info.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3 — cookies present + nav returns None → EXPIRED.
# ---------------------------------------------------------------------------


def test_check_on_startup_expired_when_nav_returns_none() -> None:
    """Cookies present but /nav returns None → state == EXPIRED.

    The ccShield anti-pattern only WARNED here. We surface the state so
    C3 (cookie refresh) can take over and the UI can prompt the user.
    """
    bili = make_mock_bili_client(get_user_info=lambda: None)
    session = AuthSession(bili, sessdata="x", bili_jct="y")

    state = run(session.check_on_startup())

    assert state == AuthState.EXPIRED
    assert session.state == AuthState.EXPIRED


# ---------------------------------------------------------------------------
# Test 4 — cookies present + nav raises AuthExpiredError → EXPIRED, no crash.
# ---------------------------------------------------------------------------


def test_check_on_startup_expired_when_nav_raises_auth_expired() -> None:
    """Defensive: if the Bili client raises AuthExpiredError instead of
    returning None, we still transition to EXPIRED — we do NOT crash."""
    bili = make_mock_bili_client()

    async def _raise() -> dict[str, Any]:
        raise AuthExpiredError("expired")

    bili.get_user_info = AsyncMock(side_effect=_raise)

    session = AuthSession(bili, sessdata="x", bili_jct="y")

    state = run(session.check_on_startup())

    assert state == AuthState.EXPIRED
    assert session.state == AuthState.EXPIRED


# ---------------------------------------------------------------------------
# Test 5 — handle_auth_expired fires callbacks; one bad callback doesn't
# block the others.
# ---------------------------------------------------------------------------


def test_handle_auth_expired_fires_all_callbacks_and_sets_state() -> None:
    """handle_auth_expired awaits every registered callback and sets state
    to EXPIRED. A callback that raises must NOT prevent the others from
    running."""
    calls: list[str] = []

    async def cb_a() -> None:
        calls.append("a")

    async def cb_b() -> None:
        calls.append("b")

    bili = make_mock_bili_client(get_user_info=lambda: {"uname": "U"})
    session = AuthSession(bili, sessdata="x", bili_jct="y")
    session.on_expired(cb_a)
    session.on_expired(cb_b)

    run(session.handle_auth_expired())

    assert session.state == AuthState.EXPIRED
    # Order of iteration is insertion order — both must run.
    assert calls == ["a", "b"]


def test_handle_auth_expired_isolates_callback_exceptions() -> None:
    """A raising callback does NOT prevent other callbacks from running.

    We deliberately swallow the exception inside handle_auth_expired — the
    callbacks are side-effect hooks (WS broadcast, UI notification); one
    flaky listener must not poison the others.
    """
    calls: list[str] = []

    async def good_first() -> None:
        calls.append("first")

    async def bad_middle() -> None:
        calls.append("middle-before-raise")
        raise RuntimeError("boom from callback")

    async def good_last() -> None:
        calls.append("last")

    bili = make_mock_bili_client(get_user_info=lambda: {"uname": "U"})
    session = AuthSession(bili, sessdata="x", bili_jct="y")
    session.on_expired(good_first)
    session.on_expired(bad_middle)
    session.on_expired(good_last)

    # Must not raise even though the middle callback raises.
    run(session.handle_auth_expired())

    assert session.state == AuthState.EXPIRED
    assert calls == ["first", "middle-before-raise", "last"]


# ---------------------------------------------------------------------------
# Test 6 — require_authenticated gating.
# ---------------------------------------------------------------------------


def _force_state(session: AuthSession, target: AuthState) -> None:
    """Test-only: bypass check_on_startup and pin the state.

    Lets the gating tests avoid a round-trip through get_user_info. We
    poke the private attribute on purpose — this is exactly the kind of
    test seam that has to live in the test module, not in production code.
    """
    session._state = target


def test_require_authenticated_passes_when_state_authenticated() -> None:
    bili = make_mock_bili_client(get_user_info=lambda: {"uname": "U"})
    session = AuthSession(bili, sessdata="x", bili_jct="y")
    _force_state(session, AuthState.AUTHENTICATED)

    # Should NOT raise.
    session.require_authenticated()


def test_require_authenticated_raises_when_state_needs_login() -> None:
    bili = make_mock_bili_client(get_user_info=lambda: None)
    session = AuthSession(bili, sessdata="", bili_jct="")
    _force_state(session, AuthState.NEEDS_LOGIN)

    with pytest.raises(NotAuthenticatedError):
        session.require_authenticated()


def test_require_authenticated_raises_when_state_expired() -> None:
    bili = make_mock_bili_client(get_user_info=lambda: None)
    session = AuthSession(bili, sessdata="x", bili_jct="y")
    _force_state(session, AuthState.EXPIRED)

    with pytest.raises(NotAuthenticatedError):
        session.require_authenticated()


# ---------------------------------------------------------------------------
# Test 7 — on_expired registration semantics.
# ---------------------------------------------------------------------------


def test_on_expired_grows_callback_list_in_insertion_order() -> None:
    """Callbacks list grows on each registration, in insertion order."""
    bili = make_mock_bili_client(get_user_info=lambda: {"uname": "U"})
    session = AuthSession(bili, sessdata="x", bili_jct="y")

    async def first() -> None:
        pass

    async def second() -> None:
        pass

    assert len(session._on_expired_callbacks) == 0

    session.on_expired(first)
    assert session._on_expired_callbacks == [first]

    session.on_expired(second)
    assert session._on_expired_callbacks == [first, second]


def test_on_expired_requires_async_callable() -> None:
    """Registered callbacks must be async (coroutine functions).

    A sync callback would silently produce a non-awaitable on the next
    handle_auth_expired call and crash. Spec mandates async.
    """
    bili = make_mock_bili_client(get_user_info=lambda: {"uname": "U"})
    session = AuthSession(bili, sessdata="x", bili_jct="y")

    async def good() -> None:
        pass

    session.on_expired(good)

    registered = session._on_expired_callbacks[-1]
    assert inspect.iscoroutinefunction(registered), (
        "on_expired must require an async callable (coroutine function)"
    )


# ---------------------------------------------------------------------------
# Default-state invariant — new AuthSession starts in NEEDS_LOGIN.
# ---------------------------------------------------------------------------


def test_initial_state_is_needs_login_before_check() -> None:
    """Before check_on_startup runs, the state is NEEDS_LOGIN.

    This is the safe default: nothing's been verified yet, so the routes
    must gate until proven otherwise.
    """
    bili = make_mock_bili_client(get_user_info=lambda: None)
    session = AuthSession(bili, sessdata="x", bili_jct="y")
    assert session.state == AuthState.NEEDS_LOGIN


# ---------------------------------------------------------------------------
# AuthState enum surface — must be exactly 3 members.
# ---------------------------------------------------------------------------


def test_auth_state_has_exactly_three_members() -> None:
    members = {m.name for m in AuthState}
    assert members == {"AUTHENTICATED", "NEEDS_LOGIN", "EXPIRED"}


# ---------------------------------------------------------------------------
# Settings fallthrough — when overrides are absent, the module reads
# from app.config.settings. Exercises the helper + _resolve_cookies branch.
# ---------------------------------------------------------------------------


def test_check_on_startup_no_overrides_reads_settings_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No overrides + empty .env → state == NEEDS_LOGIN, no /nav call.

    Exercises ``_load_cookie_settings`` + the ``_resolve_cookies`` fallthrough
    path. We point settings at a tmp .env so this is hermetic and does not
    depend on whatever the developer has on disk.
    """
    import os
    from pathlib import Path

    # Direct the pydantic-settings load at an absent file so it falls
    # through to declared defaults (all empty strings). Save+restore
    # env vars so a developer with a real SESSDATA does not leak into
    # this test.
    saved = {k: os.environ.pop(k, None) for k in ("SESSDATA", "BILI_JCT", "BUVID3")}

    try:
        # Build a Settings with a guaranteed-absent env file so we know
        # every cookie is empty. Reload the module-level ``settings``
        # to pick up our override.
        from app.config import Settings

        abs_env = Path("/tmp/reccshield_definitely_absent_for_test.env")
        if abs_env.exists():
            abs_env.unlink()
        fresh = Settings(_env_file=str(abs_env))  # pyright: ignore[reportCallIssue]

        import app.config as config_module

        monkeypatch.setattr(config_module, "settings", fresh)

        bili = make_mock_bili_client(
            get_user_info=lambda: {"uname": "U", "mid": 1}  # never reached
        )
        session = AuthSession(bili)  # no overrides — uses settings

        state = run(session.check_on_startup())

        assert state == AuthState.NEEDS_LOGIN
        bili.get_user_info.assert_not_called()
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_resolve_cookies_with_one_override_uses_settings_for_other() -> None:
    """Override one cookie, leave the other to settings.

    Exercises the mixed branch in ``_resolve_cookies``: the override wins
    for SESSDATA, settings provides the (empty) BILI_JCT — and the
    startup check still classifies as NEEDS_LOGIN.
    """
    bili = make_mock_bili_client(get_user_info=lambda: None)
    session = AuthSession(bili, sessdata="x", bili_jct=None)
    # _resolve_cookies is the internal helper; we exercise the public
    # contract through check_on_startup which calls it.
    state = run(session.check_on_startup())
    assert state == AuthState.NEEDS_LOGIN


def test_module_level_singleton_is_auth_session_instance() -> None:
    """The package exposes a module-level ``auth_session`` singleton.

    Smoke check: importing it yields an AuthSession wired to a real
    BilibiliClient (not a Mock). We do NOT call ``check_on_startup`` on
    it — that would hit the network — just inspect the type.
    """
    from app.auth.session import auth_session as singleton

    assert isinstance(singleton, AuthSession)
    assert singleton.state == AuthState.NEEDS_LOGIN


# ---------------------------------------------------------------------------
# mark_authenticated_after_login — hot-reload the bili_client cookie jar
# ---------------------------------------------------------------------------
#
# Bug fix: after a successful QR / manual login, the long-lived
# ``BilibiliClient`` singleton's httpx cookie jar MUST be refreshed
# before ``check_on_startup`` fires ``/nav`` — otherwise the jar still
# carries the import-time empty cookies, ``/nav`` returns ``-101``, and
# the state machine ends up in EXPIRED even though the cookies on disk
# are fresh.


def test_mark_authenticated_after_login_refreshes_bili_client_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mark_authenticated_after_login must push the new cookies into the
    long-lived ``bili_client`` BEFORE firing ``/nav`` via
    ``check_on_startup``.

    Mock contract: the mock's ``get_user_info`` returns the user record
    ONLY after asserting ``update_cookies`` was already called with the
    new SESSDATA / bili_jct — i.e. if ``update_cookies`` was NOT called
    (or was called with the wrong values), the assertion fires and the
    test naturally fails.
    """
    import os
    from pathlib import Path

    saved = {k: os.environ.pop(k, None) for k in ("SESSDATA", "BILI_JCT", "BUVID3")}

    try:
        from app.config import Settings

        abs_env = Path("/tmp/reccshield_test_mark_login.env")
        if abs_env.exists():
            abs_env.unlink()
        fresh = Settings(_env_file=str(abs_env))  # pyright: ignore[reportCallIssue]

        import app.config as config_module

        monkeypatch.setattr(config_module, "settings", fresh)

        bili = MagicMock(spec=["get_user_info", "update_cookies"])

        async def _nav_after_refresh() -> dict[str, Any]:
            assert bili.update_cookies.call_count >= 1
            last_call = bili.update_cookies.call_args
            assert last_call is not None
            passed_cookies: dict[str, str] = dict(last_call.args[0])
            assert passed_cookies.get("SESSDATA") == "fresh-sess"
            assert passed_cookies.get("bili_jct") == "fresh-jct"
            return {"uname": "U", "mid": 1, "isLogin": True}

        bili.get_user_info = AsyncMock(side_effect=_nav_after_refresh)
        bili.update_cookies = MagicMock(return_value=None)

        session = AuthSession(bili)

        state = run(
            session.mark_authenticated_after_login(
                sessdata="fresh-sess",
                bili_jct="fresh-jct",
                buvid3="fresh-buv",
            )
        )

        assert state == AuthState.AUTHENTICATED
        bili.update_cookies.assert_called()
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_mark_authenticated_after_login_returns_authenticated_when_nav_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: jar refresh + nav returns user record → AUTHENTICATED."""
    import os
    from pathlib import Path

    saved = {k: os.environ.pop(k, None) for k in ("SESSDATA", "BILI_JCT", "BUVID3")}

    try:
        from app.config import Settings

        abs_env = Path("/tmp/reccshield_test_mark_login2.env")
        if abs_env.exists():
            abs_env.unlink()
        fresh = Settings(_env_file=str(abs_env))  # pyright: ignore[reportCallIssue]

        import app.config as config_module

        monkeypatch.setattr(config_module, "settings", fresh)

        bili = MagicMock(spec=["get_user_info", "update_cookies"])
        bili.get_user_info = AsyncMock(
            return_value={"uname": "U", "mid": 99, "isLogin": True}
        )
        bili.update_cookies = MagicMock(return_value=None)

        session = AuthSession(bili)

        state = run(
            session.mark_authenticated_after_login(
                sessdata="new-s", bili_jct="new-j", buvid3=None
            )
        )

        assert state == AuthState.AUTHENTICATED
        bili.update_cookies.assert_called_once()
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
