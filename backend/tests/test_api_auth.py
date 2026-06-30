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
    """
    from app.auth.session import AuthSession, AuthState, NotAuthenticatedError

    session = MagicMock(spec=AuthSession)
    session.state = AuthState.NEEDS_LOGIN
    session.check_on_startup = AsyncMock(return_value=AuthState.NEEDS_LOGIN)

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
    """Successful poll → write_env_atomic called + auth_session.check_on_startup()
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

    # Snapshot the await count BEFORE the request — the lifespan calls
    # check_on_startup() on startup so the count is already ≥ 1.
    before = mock_auth_session.check_on_startup.await_count

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

    # State refresh was triggered (at least one NEW check_on_startup call
    # beyond the one fired by the lifespan).
    after = mock_auth_session.check_on_startup.await_count
    assert after > before, (
        "auth_session.check_on_startup must be re-fired after QR-poll success"
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
        client: Any,
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
        client: Any,
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
    ):
        assert path in paths, f"missing path in OpenAPI: {path}"


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
