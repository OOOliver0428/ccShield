"""TDD: tests for FastAPI auth routes + LOCAL_TOKEN middleware + host guard (T8).

Contract under test:

- /health is exempt from token + host guard.
- /openapi.json and /docs are exempt.
- /api/* and /ws/* require:
    1. Host header (host part only, strip :port) is in {"localhost", "127.0.0.1"},
       else 403.
    2. Authorization: Bearer <settings.LOCAL_TOKEN>, else 401.
       WS allows ?token=<token> as a fallback since WS clients cannot set
       custom headers reliably.
- POST /api/auth/qr/start     → {qrcode_url, qrcode_key}
- GET  /api/auth/qr/poll      → {status: "scanning"|"confirmed"|"expired"|"success"}
- GET  /api/auth/status       → {state: <AuthState.value>}
- POST /api/auth/manual       → {uname, mid} | 400 on LoginIncompleteError
- /openapi.json lists all four routes under /api/auth/.

TDD step 1: tests FIRST. They MUST fail (collection or assertion) until
app/api/ + main.py are implemented.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_auth_session(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the module-level ``auth_session`` singleton with a MagicMock.

    The lifespan in :mod:`app.main` calls
    ``await auth_session.check_on_startup()`` on startup. We mock that
    method with an ``AsyncMock`` so the lifespan never hits the network.

    The ``/api/auth/status`` endpoint reads ``auth_session.state.value`` —
    tests can flip the state by mutating ``mock_auth_session.state``.

    ``mark_authenticated_after_login`` is the post-login hot-reload
    hook used by both ``/api/auth/qr/poll`` (success) and
    ``/api/auth/manual`` (success). MagicMock(spec=…) does NOT auto
    upgrade async methods to ``AsyncMock``, so we wire that explicitly
    too — otherwise awaiting the auto-generated MagicMock would raise
    ``TypeError: object MagicMock can't be used in 'await' expression``.
    """
    from app.auth.session import AuthSession, AuthState, NotAuthenticatedError

    session = MagicMock(spec=AuthSession)
    session.state = AuthState.NEEDS_LOGIN
    session.check_on_startup = AsyncMock(return_value=AuthState.NEEDS_LOGIN)
    session.mark_authenticated_after_login = AsyncMock(
        return_value=AuthState.NEEDS_LOGIN
    )

    def _require() -> None:
        if session.state != AuthState.AUTHENTICATED:
            raise NotAuthenticatedError("not authenticated (test)")

    session.require_authenticated = _require

    # Patch the module attribute so EVERY consumer that reads
    # ``app.auth.session.auth_session`` at call time sees the mock.
    monkeypatch.setattr("app.auth.session.auth_session", session)
    return session


@pytest.fixture
def app(mock_auth_session: MagicMock) -> Any:
    """Build the FastAPI app after the auth_session mock is in place."""
    from app.main import create_app

    return create_app()


@pytest.fixture
def client(app: Any) -> Iterator[TestClient]:
    """Run the app under TestClient WITH the lifespan context manager.

    The lifespan creates the shared ``httpx.AsyncClient`` on
    ``app.state.http_client`` — without it the auth routes that depend
    on it would 500.
    """
    with TestClient(app) as c:
        yield c


def _bearer(token: str) -> dict[str, str]:
    """Build an Authorization header for the given bearer token."""
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. /health is exempt from token + host guard.
# ---------------------------------------------------------------------------


def test_health_does_not_require_token_nor_local_host(client: TestClient) -> None:
    """/health must answer 200 even with no Authorization AND an evil Host.
    This is the liveness probe — it must not be gated."""
    response = client.get("/health", headers={"Host": "evil.com"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_openapi_is_exempt_from_token_and_host_guard(client: TestClient) -> None:
    """/openapi.json is documentation — must be reachable without a token."""
    response = client.get("/openapi.json", headers={"Host": "localhost"})
    assert response.status_code == 200
    assert "paths" in response.json()


# ---------------------------------------------------------------------------
# 2-4. /api/auth/status — token gate
# ---------------------------------------------------------------------------


def test_api_auth_status_without_authorization_returns_401(client: TestClient) -> None:
    """Missing Authorization header on a protected /api route → 401."""
    response = client.get("/api/auth/status", headers={"Host": "localhost"})
    assert response.status_code == 401


def test_api_auth_status_with_wrong_bearer_returns_401(client: TestClient) -> None:
    """Authorization present but token mismatches settings.LOCAL_TOKEN → 401."""
    response = client.get(
        "/api/auth/status",
        headers={"Host": "localhost", **_bearer("definitely-not-the-right-token")},
    )
    assert response.status_code == 401


def test_api_auth_status_with_correct_bearer_returns_state(
    client: TestClient, mock_auth_session: MagicMock
) -> None:
    """Correct bearer → 200 with the current AuthState.value."""
    from app.auth.session import AuthState
    from app.config import settings

    # Pin the state on the mock so the endpoint reads a known value.
    mock_auth_session.state = AuthState.AUTHENTICATED

    response = client.get(
        "/api/auth/status",
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 200
    assert response.json() == {"state": "authenticated"}


# ---------------------------------------------------------------------------
# 5. Host guard — /api requires localhost or 127.0.0.1 (no port matters).
# ---------------------------------------------------------------------------


def test_api_with_evil_host_returns_403_even_with_correct_token(
    client: TestClient,
) -> None:
    """Host guard runs BEFORE token check — evil host with a real token → 403."""
    from app.config import settings

    response = client.get(
        "/api/auth/status",
        headers={"Host": "evil.com", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 403


def test_api_with_localhost_and_explicit_port_passes(client: TestClient) -> None:
    """Host: localhost:8000 (with port) must be accepted — port is stripped."""
    from app.config import settings

    response = client.get(
        "/api/auth/status",
        headers={"Host": "localhost:8000", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 200


def test_api_with_127_0_0_1_and_explicit_port_passes(client: TestClient) -> None:
    """Host: 127.0.0.1:5173 (Vite dev server loopback) must be accepted."""
    from app.config import settings

    response = client.get(
        "/api/auth/status",
        headers={"Host": "127.0.0.1:5173", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# 6. POST /api/auth/qr/start
# ---------------------------------------------------------------------------


def test_qr_start_returns_url_and_key_with_valid_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: qr_generate returns the B站 payload → route returns it."""
    from app.config import settings

    async def fake_qr_generate(http_client: Any) -> dict[str, str]:
        return {
            "qrcode_url": "https://i0.hdslb.com/bfs/qr.png",
            "qrcode_key": "fake_qr_key_xyz",
        }

    # Patch the name as imported by auth_routes — monkeypatch.setattr looks
    # up the attribute on the module each time the route runs.
    monkeypatch.setattr("app.api.auth_routes.qr_generate", fake_qr_generate)

    response = client.post(
        "/api/auth/qr/start",
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "qrcode_url": "https://i0.hdslb.com/bfs/qr.png",
        "qrcode_key": "fake_qr_key_xyz",
    }


# ---------------------------------------------------------------------------
# 7. GET /api/auth/qr/poll — three sub-cases
# ---------------------------------------------------------------------------


def test_qr_poll_scanning_returns_status_scanning(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QrAwaitingScanError from qr_poll → route returns {status: "scanning"}."""
    from app.bilibili.auth import QrAwaitingScanError
    from app.config import settings

    async def fake_qr_poll(http_client: Any, qrcode_key: str) -> dict[str, str]:
        raise QrAwaitingScanError()

    monkeypatch.setattr("app.api.auth_routes.qr_poll", fake_qr_poll)

    response = client.get(
        "/api/auth/qr/poll?qrcode_key=abc",
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "scanning"}


def test_qr_poll_confirmed_returns_status_confirmed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QrAwaitingConfirmError → {status: "confirmed"} (scanned, awaiting phone tap)."""
    from app.bilibili.auth import QrAwaitingConfirmError
    from app.config import settings

    async def fake_qr_poll(http_client: Any, qrcode_key: str) -> dict[str, str]:
        raise QrAwaitingConfirmError()

    monkeypatch.setattr("app.api.auth_routes.qr_poll", fake_qr_poll)

    response = client.get(
        "/api/auth/qr/poll?qrcode_key=abc",
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "confirmed"}


def test_qr_poll_expired_returns_status_expired(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QrExpiredError → {status: "expired"} (must regenerate the QR)."""
    from app.bilibili.auth import QrExpiredError
    from app.config import settings

    async def fake_qr_poll(http_client: Any, qrcode_key: str) -> dict[str, str]:
        raise QrExpiredError()

    monkeypatch.setattr("app.api.auth_routes.qr_poll", fake_qr_poll)

    response = client.get(
        "/api/auth/qr/poll?qrcode_key=abc",
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "expired"}


def test_qr_poll_success_writes_env_and_triggers_state_refresh(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mock_auth_session: MagicMock,
) -> None:
    """Successful poll → write_env_atomic called + auth_session.mark_authenticated_after_login()
    re-fired (state refresh); response body is {status: "success"}."""
    env_file = tmp_path / ".env"
    env_file.write_text("ROOM_ID=22210347\n", encoding="utf-8")

    # Redirect the .env write to our tmp file.
    from app.api import auth_routes

    monkeypatch.setattr(auth_routes, "_ENV_PATH", env_file)

    from app.config import settings

    async def fake_qr_poll(http_client: Any, qrcode_key: str) -> dict[str, str]:
        return {
            "status": "success",
            "sessdata": "new_sessdata_value",
            "bili_jct": "new_bili_jct_value",
            "dede_user_id": "987654",
        }

    monkeypatch.setattr("app.api.auth_routes.qr_poll", fake_qr_poll)

    write_calls: list[dict[str, Any]] = []

    def fake_write_env_atomic(
        sessdata: str, bili_jct: str, buvid3: str | None, env_path: Path
    ) -> None:
        write_calls.append(
            {
                "sessdata": sessdata,
                "bili_jct": bili_jct,
                "buvid3": buvid3,
                "env_path": env_path,
            }
        )

    monkeypatch.setattr("app.api.auth_routes.write_env_atomic", fake_write_env_atomic)

    # Snapshot the await count BEFORE the request — the lifespan + any
    # earlier route calls could have triggered mark_authenticated_after_login
    # already; we just want to assert it gets called ONCE MORE by this poll.
    before = mock_auth_session.mark_authenticated_after_login.await_count

    response = client.get(
        "/api/auth/qr/poll?qrcode_key=abc",
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "success"}

    # write_env_atomic was called once with the success-cookie values.
    assert len(write_calls) == 1
    call = write_calls[0]
    assert call["sessdata"] == "new_sessdata_value"
    assert call["bili_jct"] == "new_bili_jct_value"
    assert call["env_path"] == env_file

    # State refresh was triggered (at least one NEW mark_authenticated_after_login
    # call beyond whatever was fired before). The route no longer calls
    # check_on_startup directly — it goes through mark_authenticated_after_login
    # so the in-memory settings are updated first.
    after = mock_auth_session.mark_authenticated_after_login.await_count
    assert after > before, (
        "auth_session.mark_authenticated_after_login must be re-fired after QR-poll success"
    )


# ---------------------------------------------------------------------------
# 8. POST /api/auth/manual
# ---------------------------------------------------------------------------


def test_manual_returns_uname_mid_on_valid_cookies(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Happy path: save_cookies_manual returns {uname, mid} → route returns it."""
    env_file = tmp_path / ".env"
    env_file.write_text("ROOM_ID=22210347\n", encoding="utf-8")

    from app.api import auth_routes

    monkeypatch.setattr(auth_routes, "_ENV_PATH", env_file)

    from app.config import settings

    async def fake_save_cookies_manual(
        sessdata: str,
        bili_jct: str,
        buvid3: str | None,
        env_path: Path,
    ) -> dict[str, Any]:
        return {"uname": "tester", "mid": 12345}

    monkeypatch.setattr(
        "app.api.auth_routes.save_cookies_manual", fake_save_cookies_manual
    )

    response = client.post(
        "/api/auth/manual",
        json={"sessdata": "x", "bili_jct": "y"},
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 200
    assert response.json() == {"uname": "tester", "mid": 12345}


def test_manual_returns_400_on_login_incomplete(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """LoginIncompleteError → HTTP 400 (Bad Request — client gave bad cookies)."""
    env_file = tmp_path / ".env"

    from app.api import auth_routes

    monkeypatch.setattr(auth_routes, "_ENV_PATH", env_file)

    from app.bilibili.auth import LoginIncompleteError
    from app.config import settings

    async def fake_save_cookies_manual(
        sessdata: str,
        bili_jct: str,
        buvid3: str | None,
        env_path: Path,
    ) -> dict[str, Any]:
        raise LoginIncompleteError("bad cookies (test)")

    monkeypatch.setattr(
        "app.api.auth_routes.save_cookies_manual", fake_save_cookies_manual
    )

    response = client.post(
        "/api/auth/manual",
        json={"sessdata": "x", "bili_jct": "y"},
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# 9. /openapi.json contains all four auth routes
# ---------------------------------------------------------------------------


def test_openapi_lists_all_auth_routes(client: TestClient) -> None:
    """OpenAPI surface must list every /api/auth/* route the spec defines."""
    response = client.get("/openapi.json", headers={"Host": "localhost"})
    assert response.status_code == 200
    paths = response.json()["paths"]
    for path in (
        "/api/auth/qr/start",
        "/api/auth/qr/poll",
        "/api/auth/status",
        "/api/auth/manual",
        "/api/auth/bootstrap",
        "/api/auth/me",
    ):
        assert path in paths, f"missing path in OpenAPI: {path}"


# ---------------------------------------------------------------------------
# 9b. GET /api/auth/bootstrap — single-user-local token bootstrap
# ---------------------------------------------------------------------------
#
# The frontend cannot call any other /api endpoint until it knows
# LOCAL_TOKEN, but LOCAL_TOKEN is generated lazily inside the process.
# We expose it on a dedicated, host-guarded-but-token-EXEMPT endpoint so a
# browser tab on ``localhost`` can fetch the value on first load and attach
# it to all subsequent calls.
#
# Host guard still applies (no ``Host: evil.com``), so an external origin
# cannot ride the CORS allow-list + the exempt path to lift the token.
# The token's job is CSRF / DNS-rebinding defence, not secrecy from
# localhost — that is the single-user-local trust model.


def test_bootstrap_returns_token_without_authorization(client: TestClient) -> None:
    """GET /api/auth/bootstrap must answer 200 with {token: LOCAL_TOKEN}
    even with NO Authorization header. This is the chicken-and-egg escape
    hatch: the very first API call from a freshly opened SPA page.
    """
    from app.config import settings

    response = client.get("/api/auth/bootstrap", headers={"Host": "localhost"})
    assert response.status_code == 200
    assert response.json() == {"token": settings.LOCAL_TOKEN}


def test_bootstrap_rejects_non_local_host(client: TestClient) -> None:
    """Host guard still applies to bootstrap — a malicious external origin
    cannot lift the LOCAL_TOKEN even though the path is auth-exempt.
    """
    response = client.get("/api/auth/bootstrap", headers={"Host": "evil.com"})
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Extra: ensure the "scanning" test path actually maps to /api/auth/qr/poll.
# (Defends against a route-prefix typo slipping past the other tests.)
# ---------------------------------------------------------------------------


def test_qr_poll_route_path_is_correct() -> None:
    """Spot-check the route name — defends against a router prefix mistake."""
    from fastapi.routing import APIRoute

    from app.api.auth_routes import router

    paths = {route.path for route in router.routes if isinstance(route, APIRoute)}
    assert "/auth/qr/poll" in paths
    assert "/auth/qr/start" in paths
    assert "/auth/status" in paths
    assert "/auth/manual" in paths
    assert "/auth/me" in paths


# ---------------------------------------------------------------------------
# /api/auth/me — current user identity (post-login display)
# ---------------------------------------------------------------------------


def test_auth_me_returns_uname_and_mid_when_authenticated(
    client: TestClient, mock_auth_session: MagicMock
) -> None:
    """AUTHENTICATED + /nav returns data → 200 with uname/mid."""
    from app.auth.session import AuthState
    from app.config import settings

    mock_auth_session.state = AuthState.AUTHENTICATED
    mock_auth_session.get_current_user = AsyncMock(
        return_value={"uname": "tester", "mid": 12345, "isLogin": True}
    )

    response = client.get(
        "/api/auth/me",
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 200
    assert response.json() == {"uname": "tester", "mid": 12345}


def test_auth_me_returns_401_when_not_authenticated(
    client: TestClient, mock_auth_session: MagicMock
) -> None:
    """NEEDS_LOGIN / EXPIRED → 401."""
    from app.auth.session import AuthState
    from app.config import settings

    mock_auth_session.state = AuthState.NEEDS_LOGIN
    mock_auth_session.get_current_user = AsyncMock(return_value=None)

    response = client.get(
        "/api/auth/me",
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 401


def test_auth_me_returns_401_when_nav_returns_no_data(
    client: TestClient, mock_auth_session: MagicMock
) -> None:
    """AUTHENTICATED in state machine but /nav returns None → 401."""
    from app.auth.session import AuthState
    from app.config import settings

    mock_auth_session.state = AuthState.AUTHENTICATED
    mock_auth_session.get_current_user = AsyncMock(return_value=None)

    response = client.get(
        "/api/auth/me",
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 401


def test_auth_me_returns_401_when_nav_payload_malformed(
    client: TestClient, mock_auth_session: MagicMock
) -> None:
    """AUTHENTICATED + /nav returns data without uname/mid → 401."""
    from app.auth.session import AuthState
    from app.config import settings

    mock_auth_session.state = AuthState.AUTHENTICATED
    mock_auth_session.get_current_user = AsyncMock(
        return_value={"isLogin": True}
    )

    response = client.get(
        "/api/auth/me",
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 401


def test_auth_me_requires_bearer_token(
    client: TestClient, mock_auth_session: MagicMock
) -> None:
    """401 when no Authorization header — middleware gate still applies."""
    from app.auth.session import AuthState

    mock_auth_session.state = AuthState.AUTHENTICATED
    mock_auth_session.get_current_user = AsyncMock(
        return_value={"uname": "tester", "mid": 1}
    )

    response = client.get("/api/auth/me", headers={"Host": "localhost"})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# 10. Hot-reload: a successful login MUST flip /auth/status to AUTHENTICATED
#     within the SAME process. Regression: previous code only wrote the new
#     cookies to .env but never updated the in-memory ``settings`` singleton,
#     so ``check_on_startup`` kept reading the import-time empty cookies and
#     ``/auth/status`` reported ``needs_login`` until the user restarted the
#     backend.
# ---------------------------------------------------------------------------


def test_qr_poll_success_flips_auth_status_to_authenticated_in_same_process(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mock_auth_session: MagicMock,
) -> None:
    """After a successful QR-poll, ``GET /auth/status`` reports
    ``authenticated`` WITHOUT a process restart.

    Wire-up: ``mark_authenticated_after_login`` is mocked with a
    ``side_effect`` that mutates ``session.state`` to AUTHENTICATED
    and the live ``settings.SESSDATA/BILI_JCT`` to the new cookies
    — mirroring what the real method does in production.
    """
    from app.api import auth_routes
    from app.auth.session import AuthState
    from app.config import settings

    env_file = tmp_path / ".env"
    monkeypatch.setattr(auth_routes, "_ENV_PATH", env_file)

    async def fake_qr_poll(http_client: Any, qrcode_key: str) -> dict[str, str]:
        return {
            "status": "success",
            "sessdata": "fresh_sess",
            "bili_jct": "fresh_jct",
            "dede_user_id": "12345",
        }

    monkeypatch.setattr("app.api.auth_routes.qr_poll", fake_qr_poll)

    def fake_write_env_atomic(
        sessdata: str, bili_jct: str, buvid3: str | None, env_path: Path
    ) -> None:
        return None

    monkeypatch.setattr("app.api.auth_routes.write_env_atomic", fake_write_env_atomic)

    # Capture the pre-call state of the live settings module so we can
    # verify the in-memory hot-reload touched it.
    pre_sess = settings.SESSDATA
    pre_jct = settings.BILI_JCT

    # Mirror the real mark_authenticated_after_login side effect: the
    # route-layer test cannot hit the real Bili client (no network) so
    # we let the mock pretend it just verified the cookies.
    async def fake_mark(
        sessdata: str, bili_jct: str, buvid3: str | None = None
    ) -> AuthState:
        settings.SESSDATA = sessdata
        settings.BILI_JCT = bili_jct
        if buvid3 is not None:
            settings.BUVID3 = buvid3
        mock_auth_session.state = AuthState.AUTHENTICATED
        return AuthState.AUTHENTICATED

    monkeypatch.setattr(
        "app.auth.session.auth_session",
        mock_auth_session,
    )
    mock_auth_session.mark_authenticated_after_login.side_effect = fake_mark

    poll_resp = client.get(
        "/api/auth/qr/poll?qrcode_key=abc",
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert poll_resp.status_code == 200
    assert poll_resp.json() == {"status": "success"}

    assert settings.SESSDATA == "fresh_sess"
    assert settings.BILI_JCT == "fresh_jct"
    assert settings.SESSDATA != pre_sess
    assert settings.BILI_JCT != pre_jct
    assert mock_auth_session.state == AuthState.AUTHENTICATED

    status_resp = client.get(
        "/api/auth/status",
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert status_resp.status_code == 200
    assert status_resp.json() == {"state": "authenticated"}


def test_manual_success_flips_auth_status_to_authenticated_in_same_process(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mock_auth_session: MagicMock,
) -> None:
    """After a successful ``/auth/manual`` POST, ``GET /auth/status`` reports
    ``authenticated`` WITHOUT a process restart.

    Plan B: user-pasted cookies validated by ``/nav`` inside
    ``save_cookies_manual``; the route then hot-reloads the in-memory
    settings + auth state.
    """
    from app.api import auth_routes
    from app.auth.session import AuthState
    from app.config import settings

    env_file = tmp_path / ".env"
    monkeypatch.setattr(auth_routes, "_ENV_PATH", env_file)

    async def fake_save_cookies_manual(
        sessdata: str,
        bili_jct: str,
        buvid3: str | None,
        env_path: Path,
    ) -> dict[str, Any]:
        return {"uname": "tester", "mid": 12345}

    monkeypatch.setattr(
        "app.api.auth_routes.save_cookies_manual", fake_save_cookies_manual
    )

    pre_sess = settings.SESSDATA
    pre_jct = settings.BILI_JCT
    pre_buvid = settings.BUVID3

    async def fake_mark(
        sessdata: str, bili_jct: str, buvid3: str | None = None
    ) -> AuthState:
        settings.SESSDATA = sessdata
        settings.BILI_JCT = bili_jct
        if buvid3 is not None:
            settings.BUVID3 = buvid3
        mock_auth_session.state = AuthState.AUTHENTICATED
        return AuthState.AUTHENTICATED

    monkeypatch.setattr(
        "app.auth.session.auth_session",
        mock_auth_session,
    )
    mock_auth_session.mark_authenticated_after_login.side_effect = fake_mark

    manual_resp = client.post(
        "/api/auth/manual",
        json={"sessdata": "manual_sess", "bili_jct": "manual_jct", "buvid3": "manual_buv"},
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert manual_resp.status_code == 200
    assert manual_resp.json() == {"uname": "tester", "mid": 12345}

    assert settings.SESSDATA == "manual_sess"
    assert settings.BILI_JCT == "manual_jct"
    assert settings.BUVID3 == "manual_buv"
    assert settings.SESSDATA != pre_sess
    assert settings.BILI_JCT != pre_jct
    assert settings.BUVID3 != pre_buvid
    assert mock_auth_session.state == AuthState.AUTHENTICATED

    status_resp = client.get(
        "/api/auth/status",
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert status_resp.status_code == 200
    assert status_resp.json() == {"state": "authenticated"}


def test_manual_success_flips_auth_status_even_when_buvid3_omitted(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mock_auth_session: MagicMock,
) -> None:
    """``/auth/manual`` with no ``buvid3`` still flips state — and leaves
    any pre-existing ``settings.BUVID3`` untouched (we don't blank it
    out just because the user didn't paste one this round AND /spi
    failed to yield one).

    Defensive: ``mark_authenticated_after_login`` treats ``buvid3=None``
    as "do not touch the in-memory value". The route's Bug-2 /spi-fetch
    fallback (in production) only runs when the user omitted buvid3;
    when that fetch ALSO returns None the in-memory BUVID3 must be
    preserved. This test pins that contract so a future change to
    eager-clear isn't silently introduced.
    """
    from app.api import auth_routes
    from app.auth.session import AuthState
    from app.config import settings

    env_file = tmp_path / ".env"
    monkeypatch.setattr(auth_routes, "_ENV_PATH", env_file)

    # Seed BUVID3 so we can assert the manual flow that omits buvid3
    # leaves the in-memory value intact. Mirrors a real session that
    # had a buvid3 from a previous QR scan.
    settings.BUVID3 = "preexisting_buvid3"

    async def fake_save_cookies_manual(
        sessdata: str,
        bili_jct: str,
        buvid3: str | None,
        env_path: Path,
    ) -> dict[str, Any]:
        return {"uname": "tester", "mid": 12345}

    monkeypatch.setattr(
        "app.api.auth_routes.save_cookies_manual", fake_save_cookies_manual
    )

    # Bug 2 / F3 — /spi fallback in the route. The pre-existing test was
    # written before the route fetched buvid3 from /spi; the contract
    # being pinned here is the COMBINED behaviour "user omitted buvid3
    # AND /spi returned nothing → leave the in-memory value alone", so
    # we mock fetch_buvid3 to return None to simulate the /spi-failed
    # branch. A separate test (`test_manual_cookies_fetches_buvid3_...`)
    # covers the /spi-succeeded branch.
    async def fake_fetch_buvid3(http_client: Any) -> str | None:
        return None

    monkeypatch.setattr("app.api.auth_routes.fetch_buvid3", fake_fetch_buvid3)

    async def fake_mark(
        sessdata: str, bili_jct: str, buvid3: str | None = None
    ) -> AuthState:
        settings.SESSDATA = sessdata
        settings.BILI_JCT = bili_jct
        if buvid3 is not None:
            settings.BUVID3 = buvid3
        mock_auth_session.state = AuthState.AUTHENTICATED
        return AuthState.AUTHENTICATED

    monkeypatch.setattr("app.auth.session.auth_session", mock_auth_session)
    mock_auth_session.mark_authenticated_after_login.side_effect = fake_mark

    try:
        manual_resp = client.post(
            "/api/auth/manual",
            json={"sessdata": "manual_sess2", "bili_jct": "manual_jct2"},
            headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
        )
        assert manual_resp.status_code == 200
        assert manual_resp.json() == {"uname": "tester", "mid": 12345}

        assert settings.BUVID3 == "preexisting_buvid3"
        assert settings.SESSDATA == "manual_sess2"
        assert settings.BILI_JCT == "manual_jct2"
        assert mock_auth_session.state == AuthState.AUTHENTICATED
    finally:
        # Module-level settings leaks across tests; restore to defaults
        # so unrelated suites see a clean slate.
        settings.SESSDATA = ""
        settings.BILI_JCT = ""
        settings.BUVID3 = None


# ---------------------------------------------------------------------------
# Bug 1 / F3: auth_routes._ENV_PATH MUST equal app.config._ENV_FILE so the
# QR flow writes to the SAME .env the config loader reads on startup.
# Before the fix, auth_routes computed _PROJECT_ROOT from one extra hop,
# so write_env_atomic wrote to backend/.env while config.py read
# <repo>/.env → on process restart the cookies were gone and the user
# had to re-scan. Pin the single-source-of-truth invariant.
# ---------------------------------------------------------------------------


def test_env_path_matches_config_env_file_single_source_of_truth() -> None:
    """Bug 1 regression: auth_routes._ENV_PATH is the SAME Path object as
    app.config._ENV_FILE. A future refactor that re-introduces a divergent
    computation (4 parents instead of 3) breaks this test immediately.
    """
    from app import config as config_module
    from app.api import auth_routes

    assert auth_routes._ENV_PATH == config_module._ENV_FILE
    # And specifically NOT the buggy path (backend/.env).
    assert auth_routes._ENV_PATH.name == ".env"
    # The path must resolve ABOVE the backend/ package dir — i.e. the
    # project root, where pydantic-settings expects to find the file.
    assert "backend" not in auth_routes._ENV_PATH.parts


def test_qr_poll_success_passes_buvid3_from_spi_to_write_env_and_state_refresh(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mock_auth_session: MagicMock,
) -> None:
    """Bug 2 / F3 regression: successful QR poll must fetch buvid3 via
    /x/frontend/finger/spi and thread it through BOTH
    write_env_atomic AND mark_authenticated_after_login. Without buvid3 the
    /xlive/getDanmuInfo WBI call fails (need device fingerprint).
    """
    env_file = tmp_path / ".env"
    from app.api import auth_routes

    monkeypatch.setattr(auth_routes, "_ENV_PATH", env_file)

    async def fake_qr_poll(http_client: Any, qrcode_key: str) -> dict[str, str]:
        return {
            "status": "success",
            "sessdata": "qr_sess_xyz",
            "bili_jct": "qr_jct_xyz",
            "dede_user_id": "11111",
        }

    monkeypatch.setattr("app.api.auth_routes.qr_poll", fake_qr_poll)

    # /spi returns a buvid3
    async def fake_fetch_buvid3(http_client: Any) -> str | None:
        return "BUVID3_FROM_SPI"

    monkeypatch.setattr("app.api.auth_routes.fetch_buvid3", fake_fetch_buvid3)

    write_calls: list[dict[str, Any]] = []

    def fake_write_env_atomic(
        sessdata: str, bili_jct: str, buvid3: str | None, env_path: Path
    ) -> None:
        write_calls.append(
            {"sessdata": sessdata, "bili_jct": bili_jct, "buvid3": buvid3}
        )

    monkeypatch.setattr("app.api.auth_routes.write_env_atomic", fake_write_env_atomic)

    from app.auth.session import AuthState
    from app.config import settings

    async def fake_mark(
        sessdata: str, bili_jct: str, buvid3: str | None = None
    ) -> AuthState:
        settings.SESSDATA = sessdata
        settings.BILI_JCT = bili_jct
        if buvid3 is not None:
            settings.BUVID3 = buvid3
        mock_auth_session.state = AuthState.AUTHENTICATED
        return AuthState.AUTHENTICATED

    monkeypatch.setattr("app.auth.session.auth_session", mock_auth_session)
    mock_auth_session.mark_authenticated_after_login.side_effect = fake_mark

    poll = client.get(
        "/api/auth/qr/poll?qrcode_key=abc",
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert poll.status_code == 200
    assert poll.json() == {"status": "success"}

    # write_env_atomic was called with buvid3 from /spi (not None).
    assert len(write_calls) == 1
    assert write_calls[0]["sessdata"] == "qr_sess_xyz"
    assert write_calls[0]["bili_jct"] == "qr_jct_xyz"
    assert write_calls[0]["buvid3"] == "BUVID3_FROM_SPI"

    # mark_authenticated_after_login received the same buvid3 → settings.BUVID3 set.
    assert settings.BUVID3 == "BUVID3_FROM_SPI"


def test_qr_poll_success_when_spi_fails_still_succeeds(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mock_auth_session: MagicMock,
) -> None:
    """Best-effort: if /spi returns None (network blip / B站 down), the
    QR login still completes with SESSDATA+bili_jct. buvid3=None is fine —
    the /env value of BUVID3 is preserved (write_env_atomic skips it on None).
    """
    env_file = tmp_path / ".env"
    from app.api import auth_routes

    monkeypatch.setattr(auth_routes, "_ENV_PATH", env_file)

    async def fake_qr_poll(http_client: Any, qrcode_key: str) -> dict[str, str]:
        return {
            "status": "success",
            "sessdata": "sess",
            "bili_jct": "jct",
            "dede_user_id": "1",
        }

    monkeypatch.setattr("app.api.auth_routes.qr_poll", fake_qr_poll)

    async def fake_fetch_buvid3(http_client: Any) -> str | None:
        return None  # /spi down

    monkeypatch.setattr("app.api.auth_routes.fetch_buvid3", fake_fetch_buvid3)

    write_calls: list[dict[str, Any]] = []

    def fake_write_env_atomic(
        sessdata: str, bili_jct: str, buvid3: str | None, env_path: Path
    ) -> None:
        write_calls.append(
            {"sessdata": sessdata, "bili_jct": bili_jct, "buvid3": buvid3}
        )

    monkeypatch.setattr("app.api.auth_routes.write_env_atomic", fake_write_env_atomic)

    from app.auth.session import AuthState
    from app.config import settings

    async def fake_mark(
        sessdata: str, bili_jct: str, buvid3: str | None = None
    ) -> AuthState:
        mock_auth_session.state = AuthState.AUTHENTICATED
        return AuthState.AUTHENTICATED

    monkeypatch.setattr("app.auth.session.auth_session", mock_auth_session)
    mock_auth_session.mark_authenticated_after_login.side_effect = fake_mark

    poll = client.get(
        "/api/auth/qr/poll?qrcode_key=abc",
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert poll.status_code == 200
    assert len(write_calls) == 1
    # buvid3=None on the write call (write_env_atomic leaves existing BUVID3 alone).
    assert write_calls[0]["buvid3"] is None
    # The mark call also got buvid3=None.
    mark_call = mock_auth_session.mark_authenticated_after_login.call_args
    assert mark_call is not None
    assert mark_call.kwargs.get("buvid3") is None or mark_call.args[2] is None


def test_manual_cookies_fetches_buvid3_when_user_did_not_provide_one(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mock_auth_session: MagicMock,
) -> None:
    """Plan B (manual cookies): if the user pasted SESSDATA+bili_jct but
    did NOT paste buvid3, the route should fetch it via /spi and persist
    it. B站 /nav validation will otherwise succeed but /spi-derived buvid3
    is still the right place to seed a fresh fingerprint.
    """
    env_file = tmp_path / ".env"
    from app.api import auth_routes

    monkeypatch.setattr(auth_routes, "_ENV_PATH", env_file)

    async def fake_save_cookies_manual(
        sessdata: str,
        bili_jct: str,
        buvid3: str | None,
        env_path: Path,
    ) -> dict[str, Any]:
        return {"uname": "tester", "mid": 99}

    monkeypatch.setattr(
        "app.api.auth_routes.save_cookies_manual", fake_save_cookies_manual
    )

    fetch_calls: list[bool] = []

    async def fake_fetch_buvid3(http_client: Any) -> str | None:
        fetch_calls.append(True)
        return "SPI_BUVID3_FOR_MANUAL"

    monkeypatch.setattr("app.api.auth_routes.fetch_buvid3", fake_fetch_buvid3)

    from app.auth.session import AuthState
    from app.config import settings

    async def fake_mark(
        sessdata: str, bili_jct: str, buvid3: str | None = None
    ) -> AuthState:
        settings.SESSDATA = sessdata
        settings.BILI_JCT = bili_jct
        if buvid3 is not None:
            settings.BUVID3 = buvid3
        mock_auth_session.state = AuthState.AUTHENTICATED
        return AuthState.AUTHENTICATED

    monkeypatch.setattr("app.auth.session.auth_session", mock_auth_session)
    mock_auth_session.mark_authenticated_after_login.side_effect = fake_mark

    response = client.post(
        "/api/auth/manual",
        json={"sessdata": "msess", "bili_jct": "mjct"},  # no buvid3
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 200
    # /spi was called exactly once.
    assert len(fetch_calls) == 1
    # The mark call received the spi-derived buvid3.
    mark_call = mock_auth_session.mark_authenticated_after_login.call_args
    assert mark_call is not None
    buvid3_passed = mark_call.kwargs.get("buvid3")
    if buvid3_passed is None and len(mark_call.args) >= 3:
        buvid3_passed = mark_call.args[2]
    assert buvid3_passed == "SPI_BUVID3_FOR_MANUAL"


def test_manual_cookies_skips_spi_when_user_provided_buvid3(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mock_auth_session: MagicMock,
) -> None:
    """Plan B: if the user DID paste buvid3, don't call /spi — trust the
    user's input verbatim (it's the cookie they extracted from a real
    browser session, which is what B站 expects).
    """
    env_file = tmp_path / ".env"
    from app.api import auth_routes

    monkeypatch.setattr(auth_routes, "_ENV_PATH", env_file)

    async def fake_save_cookies_manual(
        sessdata: str,
        bili_jct: str,
        buvid3: str | None,
        env_path: Path,
    ) -> dict[str, Any]:
        return {"uname": "tester", "mid": 99}

    monkeypatch.setattr(
        "app.api.auth_routes.save_cookies_manual", fake_save_cookies_manual
    )

    fetch_calls: list[bool] = []

    async def fake_fetch_buvid3(http_client: Any) -> str | None:
        fetch_calls.append(True)
        return "SHOULD_NOT_BE_USED"

    monkeypatch.setattr("app.api.auth_routes.fetch_buvid3", fake_fetch_buvid3)

    from app.auth.session import AuthState
    from app.config import settings

    async def fake_mark(
        sessdata: str, bili_jct: str, buvid3: str | None = None
    ) -> AuthState:
        mock_auth_session.state = AuthState.AUTHENTICATED
        return AuthState.AUTHENTICATED

    monkeypatch.setattr("app.auth.session.auth_session", mock_auth_session)
    mock_auth_session.mark_authenticated_after_login.side_effect = fake_mark

    response = client.post(
        "/api/auth/manual",
        json={"sessdata": "ms", "bili_jct": "mj", "buvid3": "USER_BUVID3"},
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 200
    # /spi was NOT called — user's buvid3 was used directly.
    assert len(fetch_calls) == 0
