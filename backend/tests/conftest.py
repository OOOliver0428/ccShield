"""Pytest configuration and shared fixtures for backend tests.

Ensures test isolation from the project-root ``.env``. The user may have
captured real ``SESSDATA`` + ``BILI_JCT`` cookies via the QR login flow on
their own machine — those would otherwise leak into module-level
singletons at import time and cause ``auth_session.check_on_startup`` to
make real network calls during the ``TestClient`` lifespan. That raises
``RuntimeError: Event loop is closed`` because the long-lived
``httpx.AsyncClient`` carried by ``BilibiliClient`` becomes bound to the
previous (now-closed) event loop.

The autouse fixture below empties the cookies on
``app.config.settings`` and resets the ``auth_session`` singleton's
state to ``NEEDS_LOGIN`` before every test, then restores them after.

After this fixture, every test runs the same way it would on a machine
with an absent ``.env`` — no product code changes.
"""
from __future__ import annotations

import collections.abc

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(
    monkeypatch: pytest.MonkeyPatch,
) -> collections.abc.Generator[None, None, None]:
    """Isolate tests from the real project-root ``.env`` cookies.

    Snapshot the live cookie values, force them empty, reset the
    ``auth_session`` singleton's mutable per-test state, then restore
    everything after the test. ``monkeypatch`` is also exposed so any
    ``setattr`` a test does on top of us is automatically undone before
    our teardown runs.
    """
    from app.auth.session import AuthState, auth_session
    from app.config import settings

    # 1. Snapshot the real cookies so we can restore them after the test.
    real_sessdata = settings.SESSDATA
    real_bili_jct = settings.BILI_JCT
    real_buvid3 = settings.BUVID3

    # 2. Force cookies to empty. Anything reading from ``settings`` now
    #    sees "not logged in" — the same shape as a cold start with no
    #    ``.env``. The lifespan's ``check_on_startup`` short-circuits on
    #    empty cookies (no ``/nav`` call) which prevents httpx from
    #    binding to a per-test event loop and breaking the next test.
    settings.SESSDATA = ""
    settings.BILI_JCT = ""
    settings.BUVID3 = None

    # 3. Reset the singleton. It was constructed at module-import time
    #    with the real ``.env``'s cookies; even though we've now emptied
    #    ``settings``, an earlier lifespan invocation may have already
    #    flipped its state to AUTHENTICATED. Force the fresh-cold-start
    #    shape: NEEDS_LOGIN, no overrides, no leftover callbacks.
    auth_session._state = AuthState.NEEDS_LOGIN
    auth_session._sessdata_override = None
    auth_session._bili_jct_override = None
    auth_session._on_expired_callbacks.clear()

    yield

    # 4. Restore the real cookies. monkeypatch teardown has already
    #    undone any ``setattr`` a test did on top of us, so this lands
    #    on the original values regardless of test body mutations.
    settings.SESSDATA = real_sessdata
    settings.BILI_JCT = real_bili_jct
    settings.BUVID3 = real_buvid3
