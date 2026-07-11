"""TDD tests for T18 — ban routes + WS banlist bridge.

Contract under test:

* ``POST   /api/ban`` {room_id, uid, hour, reason?} → 200 ``{ok:true}``
  on success, 400 if ``bili_client.ban_user`` returns ``False``.
  Map exceptions:
  - :class:`AuthExpiredError`     → 401 + ``auth_session.handle_auth_expired()``
  - :class:`PermissionDeniedError` → 403
  - :class:`RateLimitedError`     → 429
  - other :class:`BiliApiError`   → 502
* ``DELETE /api/ban`` {room_id, block_id, uid} → 200 on success; same mapping.
* ``GET    /api/ban-list/{room_id}`` → returns the current ban list
  (from the running manager if any, else via ``bili_client.get_ban_list``).
* ``WS     /api/ws/rooms/{room_id}/banlist`` → accepts; sends a
  ``{event:"snapshot",...}`` message on connect; pushes ``ban_added``
  / ``ban_removed`` deltas as the manager broadcasts them.
* ``/openapi.json`` lists ``/api/ban`` and ``/api/ban-list/{room_id}``.

Mock strategy (T13/T17 precedent):

* ``app.api.ban_routes._get_bili_client`` — module-level lazy factory;
  tests monkeypatch it so REST/WS handlers see an ``AsyncMock``
  without constructing a real ``BilibiliClient``.
* ``app.api.ban_routes.banlist_manager`` — module-level singleton
  binding; tests monkeypatch it with a ``_FakeBanListManager`` that
  tracks ``on_ban`` / ``on_unban`` / ``start`` / subscribe calls.
* ``app.auth.session.auth_session`` — tests swap the singleton via
  ``monkeypatch.setattr`` so ``require_authenticated`` /
  ``handle_auth_expired`` are observable.

All endpoints live under the ``LocalTokenMiddleware`` (T8) — tests set
``Host: localhost`` and the LOCAL_TOKEN bearer (or ``?token=`` for WS).

Adversarial coverage:

* WS disconnect → ``unsubscribe`` (no callback leak).
* ``on_ban`` mock side-effect must surface as a ``{event:"ban_added"}``
  message on the WS — not just ``subscribe`` returning cleanly.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.auth.session import AuthSession, AuthState, NotAuthenticatedError
from app.bilibili.client import BilibiliClient
from app.bilibili.exceptions import (
    AuthExpiredError,
    PermissionDeniedError,
    RateLimitedError,
)
from app.config import settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bearer(token: str) -> dict[str, str]:
    """Authorization header for the LOCAL_TOKEN bearer guard."""
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_auth_session(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the module-level ``auth_session`` singleton with a mock.

    Defaults to ``AUTHENTICATED`` so ``require_authenticated`` is a no-op.
    Tests can flip ``state`` to ``EXPIRED`` / ``NEEDS_LOGIN`` to exercise
    the gating; they can also inspect ``handle_auth_expired`` for the
    AuthExpiredError path.
    """
    session = MagicMock(spec=AuthSession)
    session.state = AuthState.AUTHENTICATED
    session.check_on_startup = AsyncMock(return_value=AuthState.AUTHENTICATED)
    session.handle_auth_expired = AsyncMock(return_value=None)

    def _require() -> None:
        if session.state != AuthState.AUTHENTICATED:
            raise NotAuthenticatedError(
                f"authentication required (current state: {session.state.value})"
            )

    session.require_authenticated = _require
    monkeypatch.setattr("app.auth.session.auth_session", session)
    return session


class _FakeBanListManager:
    """Drop-in replacement for :class:`BanListManager` for tests.

    Mirrors only the surface the route actually uses:
    :meth:`start`, :meth:`subscribe`, :meth:`unsubscribe`,
    :meth:`on_ban`, :meth:`on_unban`. Tracks every call so the
    route can be asserted on directly without a real ``BilibiliClient``.

    The ``_bans`` attribute is exposed as a property that aliases the
    public ``bans`` dict — the real :class:`BanListManager` keeps its
    state in ``_bans`` and the route reads that attribute directly, so
    the fake must too (tests can still mutate ``bans`` ergonomically).
    """

    def __init__(self) -> None:
        self._room_id: int | None = None
        self._subscribers: list[Any] = []
        self.on_ban_calls: list[tuple[int, dict[str, Any]]] = []
        self.on_unban_calls: list[int] = []
        self.start_calls: list[int] = []
        self.stop_calls: int = 0
        self.refresh_calls: int = 0
        # Pre-populated bans; tests can mutate to simulate reconcile.
        self.bans: dict[int, dict[str, Any]] = {}

    @property
    def _bans(self) -> dict[int, dict[str, Any]]:
        """Alias for :attr:`bans` matching the real manager's attribute name."""
        return self.bans

    async def start(self, room_id: int, is_running: Any) -> None:
        self.start_calls.append(room_id)
        self._room_id = room_id

    async def stop(self) -> None:
        self.stop_calls += 1
        self._room_id = None
        self._subscribers.clear()

    async def refresh(self, *, preserve_pending: bool = True) -> list[dict[str, Any]]:
        self.refresh_calls += 1
        return list(self.bans.values())

    async def subscribe(self, cb: Any) -> None:
        self._subscribers.append(cb)
        # Mirror T17: send the current snapshot to the new subscriber.
        await cb({"event": "snapshot", "bans": list(self.bans.values())})

    async def unsubscribe(self, cb: Any) -> None:
        with __import__("contextlib").suppress(ValueError):
            self._subscribers.remove(cb)

    async def on_ban(self, uid: int, ban_entry: dict[str, Any]) -> None:
        self.on_ban_calls.append((uid, ban_entry))
        self.bans[uid] = ban_entry
        for cb in list(self._subscribers):
            await cb({"event": "ban_added", "ban": ban_entry})

    async def on_unban(self, uid: int) -> None:
        self.on_unban_calls.append(uid)
        self.bans.pop(uid, None)
        for cb in list(self._subscribers):
            await cb({"event": "ban_removed", "uid": uid})


@pytest.fixture(autouse=True)
def _isolated_banlist_singleton() -> Iterator[None]:
    """Reset both module-level ``banlist_manager`` bindings per test.

    The WS route lazily creates a manager; without this fixture a test
    that ran the WS would leak its manager into the next test.
    """
    from app.api import ban_routes
    from app.room import banlist as bl_mod

    ban_routes.banlist_manager = None
    bl_mod.banlist_manager = None
    bl_mod.set_banlist_manager(None)
    yield
    ban_routes.banlist_manager = None
    bl_mod.banlist_manager = None
    bl_mod.set_banlist_manager(None)


@pytest.fixture
def fake_banlist_manager(monkeypatch: pytest.MonkeyPatch) -> _FakeBanListManager:
    """Replace ``ban_routes.banlist_manager`` with a fake (not started).

    Tests that need a running manager call ``mgr.start(room_id, ...)``
    or set ``_room_id`` directly.
    """
    from app.api import ban_routes

    mgr = _FakeBanListManager()
    monkeypatch.setattr(ban_routes, "banlist_manager", mgr)
    return mgr


@pytest.fixture
def fake_bili_client(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace ``ban_routes._get_bili_client`` to return a mock.

    Returns the mock itself so individual tests can wire ``side_effect``
    or ``return_value`` for the methods the route calls.
    """
    from app.api import ban_routes

    client = AsyncMock(spec=BilibiliClient)

    def _factory() -> AsyncMock:
        return client

    monkeypatch.setattr(ban_routes, "_get_bili_client", _factory)
    return client


@pytest.fixture
def app(mock_auth_session: MagicMock) -> Any:
    """Build the FastAPI app after the auth_session mock is in place."""
    from app.main import create_app

    return create_app()


@pytest.fixture
def client(app: Any) -> Iterator[TestClient]:
    """Run the app under TestClient WITH the lifespan context manager."""
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# 1. POST /api/ban (ban_user → True, manager running) → 200 + on_ban called.
# ---------------------------------------------------------------------------


def test_post_ban_returns_200_and_calls_on_ban_when_manager_running(
    client: TestClient,
    fake_bili_client: AsyncMock,
    fake_banlist_manager: _FakeBanListManager,
) -> None:
    """Happy path: ban_user=True + manager running for same room → 200 + on_ban."""
    fake_bili_client.ban_user = AsyncMock(return_value=True)
    # Manager "running" for the same room so the route takes the
    # broadcast branch.
    fake_banlist_manager._room_id = 22210347

    response = client.post(
        "/api/ban",
        json={
            "room_id": 22210347,
            "uid": 42,
            "hour": 1,
            "reason": "spam",
        },
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    fake_bili_client.ban_user.assert_awaited_once_with(22210347, 42, 1, "spam")
    assert len(fake_banlist_manager.on_ban_calls) == 1
    uid, entry = fake_banlist_manager.on_ban_calls[0]
    assert uid == 42
    assert entry["uid"] == 42
    assert entry["hour"] == 1
    assert entry["pending"] is True
    assert entry["block_id"] is None
    assert fake_banlist_manager.refresh_calls == 1


# ---------------------------------------------------------------------------
# 2. POST /api/ban (ban_user → False) → 400.
# ---------------------------------------------------------------------------


def test_post_ban_returns_400_when_ban_user_returns_false(
    client: TestClient,
    fake_bili_client: AsyncMock,
    fake_banlist_manager: _FakeBanListManager,
) -> None:
    """ban_user=False → 400; on_ban MUST NOT be called."""
    fake_bili_client.ban_user = AsyncMock(return_value=False)
    fake_banlist_manager._room_id = 22210347

    response = client.post(
        "/api/ban",
        json={"room_id": 22210347, "uid": 42, "hour": 1},
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 400
    assert fake_banlist_manager.on_ban_calls == []


@pytest.mark.parametrize("hour", [0, 1, 24, 168, 720])
def test_post_ban_accepts_only_supported_duration_levels(
    client: TestClient,
    fake_bili_client: AsyncMock,
    fake_banlist_manager: _FakeBanListManager,
    hour: int,
) -> None:
    fake_bili_client.ban_user = AsyncMock(return_value=True)
    fake_banlist_manager._room_id = None

    response = client.post(
        "/api/ban",
        json={"room_id": 22210347, "uid": 42, "hour": hour},
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )

    assert response.status_code == 200
    fake_bili_client.ban_user.assert_awaited_once_with(
        22210347, 42, hour, ""
    )


@pytest.mark.parametrize("hour", [-1, 2, 23, 721])
def test_post_ban_rejects_unsupported_duration_without_upstream_call(
    client: TestClient,
    fake_bili_client: AsyncMock,
    hour: int,
) -> None:
    fake_bili_client.ban_user = AsyncMock(return_value=True)

    response = client.post(
        "/api/ban",
        json={"room_id": 22210347, "uid": 42, "hour": hour},
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )

    assert response.status_code == 422
    fake_bili_client.ban_user.assert_not_awaited()


def test_post_ban_rejects_reason_longer_than_200_chars(
    client: TestClient,
    fake_bili_client: AsyncMock,
) -> None:
    fake_bili_client.ban_user = AsyncMock(return_value=True)
    response = client.post(
        "/api/ban",
        json={
            "room_id": 22210347,
            "uid": 42,
            "hour": 1,
            "reason": "x" * 201,
        },
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 422
    fake_bili_client.ban_user.assert_not_awaited()


# ---------------------------------------------------------------------------
# 3. POST /api/ban (AuthExpiredError) → 401 + handle_auth_expired called.
# ---------------------------------------------------------------------------


def test_post_ban_auth_expired_returns_401_and_fires_handle_auth_expired(
    client: TestClient,
    fake_bili_client: AsyncMock,
    fake_banlist_manager: _FakeBanListManager,
    mock_auth_session: MagicMock,
) -> None:
    """ban_user raises AuthExpiredError → 401 + auth_session.handle_auth_expired."""
    fake_bili_client.ban_user = AsyncMock(side_effect=AuthExpiredError("expired"))
    fake_banlist_manager._room_id = 22210347

    response = client.post(
        "/api/ban",
        json={"room_id": 22210347, "uid": 42, "hour": 1},
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 401
    mock_auth_session.handle_auth_expired.assert_awaited_once()


# ---------------------------------------------------------------------------
# 4. POST /api/ban (PermissionDeniedError → 403, RateLimitedError → 429).
# ---------------------------------------------------------------------------


def test_post_ban_permission_denied_returns_403(
    client: TestClient,
    fake_bili_client: AsyncMock,
    fake_banlist_manager: _FakeBanListManager,
) -> None:
    """ban_user raises PermissionDeniedError → 403."""
    fake_bili_client.ban_user = AsyncMock(
        side_effect=PermissionDeniedError("not a moderator")
    )
    fake_banlist_manager._room_id = 22210347

    response = client.post(
        "/api/ban",
        json={"room_id": 22210347, "uid": 42, "hour": 1},
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 403


def test_post_ban_rate_limited_returns_429(
    client: TestClient,
    fake_bili_client: AsyncMock,
    fake_banlist_manager: _FakeBanListManager,
) -> None:
    """ban_user raises RateLimitedError → 429."""
    fake_bili_client.ban_user = AsyncMock(side_effect=RateLimitedError("throttled"))
    fake_banlist_manager._room_id = 22210347

    response = client.post(
        "/api/ban",
        json={"room_id": 22210347, "uid": 42, "hour": 1},
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 429


# ---------------------------------------------------------------------------
# 5. DELETE /api/ban (unban_user → True) → 200 + on_unban called.
# ---------------------------------------------------------------------------


def test_delete_ban_returns_200_and_calls_on_unban(
    client: TestClient,
    fake_bili_client: AsyncMock,
    fake_banlist_manager: _FakeBanListManager,
) -> None:
    """DELETE /api/ban with unban_user=True → 200 + on_unban called with uid."""
    fake_bili_client.unban_user = AsyncMock(return_value=True)
    fake_banlist_manager._room_id = 22210347

    response = client.request(
        "DELETE",
        "/api/ban",
        json={"room_id": 22210347, "block_id": 99, "uid": 42},
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    fake_bili_client.unban_user.assert_awaited_once_with(22210347, 99)
    assert fake_banlist_manager.on_unban_calls == [42]


# ---------------------------------------------------------------------------
# 6. GET /api/ban-list/{room_id} → returns bans list.
# ---------------------------------------------------------------------------


def test_get_ban_list_returns_manager_state_when_running(
    client: TestClient,
    fake_bili_client: AsyncMock,
    fake_banlist_manager: _FakeBanListManager,
) -> None:
    """When the manager is running, GET reads from its local state."""
    fake_banlist_manager._room_id = 22210347
    fake_banlist_manager.bans = {
        1: {"uid": 1, "hour": 1},
        2: {"uid": 2, "hour": 2},
    }

    response = client.get(
        "/api/ban-list/22210347",
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["room_id"] == 22210347
    assert len(body["bans"]) == 2
    uids = sorted(b["uid"] for b in body["bans"])
    assert uids == [1, 2]
    assert all(set(entry) == {
        "block_id",
        "uid",
        "uname",
        "hour",
        "reason",
        "created_at",
        "expires_at",
        "pending",
    } for entry in body["bans"])
    # Manager state path — bili_client.get_ban_list MUST NOT have been
    # called (would have hit the network).
    fake_bili_client.get_ban_list.assert_not_awaited()


def test_get_ban_list_falls_back_to_bili_client_when_manager_not_running(
    client: TestClient,
    fake_bili_client: AsyncMock,
    fake_banlist_manager: _FakeBanListManager,
) -> None:
    """When the manager is NOT running, GET falls back to bili_client."""
    # fake_banlist_manager._room_id stays None → not running.
    fake_bili_client.get_ban_list = AsyncMock(
        return_value=[{"uid": 7, "hour": 3}]
    )

    response = client.get(
        "/api/ban-list/22210347",
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["room_id"] == 22210347
    assert body["bans"] == [
        {
            "block_id": None,
            "uid": 7,
            "uname": "",
            "hour": 3,
            "reason": "",
            "created_at": None,
            "expires_at": None,
            "pending": False,
        }
    ]
    fake_bili_client.get_ban_list.assert_awaited_once_with(22210347)


def test_get_ban_list_refresh_true_forces_running_manager_refresh(
    client: TestClient,
    fake_bili_client: AsyncMock,
    fake_banlist_manager: _FakeBanListManager,
) -> None:
    fake_banlist_manager._room_id = 22210347
    fake_banlist_manager.bans = {
        8: {
            "block_id": 88,
            "uid": 8,
            "uname": "fresh",
            "hour": 24,
            "reason": "spam",
            "created_at": 1700000000,
            "expires_at": 1700086400,
            "pending": False,
        }
    }

    response = client.get(
        "/api/ban-list/22210347?refresh=true",
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )

    assert response.status_code == 200
    assert response.json()["bans"][0]["block_id"] == 88
    assert fake_banlist_manager.refresh_calls == 1
    fake_bili_client.get_ban_list.assert_not_awaited()


def test_get_ban_list_normalizes_bilibili_field_aliases(
    client: TestClient,
    fake_bili_client: AsyncMock,
    fake_banlist_manager: _FakeBanListManager,
) -> None:
    fake_banlist_manager._room_id = None
    fake_bili_client.get_ban_list = AsyncMock(
        return_value=[
            {
                "id": "91",
                "uid": "305721696",
                "tuid": "9",
                "tname": "alias-user",
                "msg": "manual reason",
                "ctime": "2026-07-10 19:00:00",
                "block_end_time": "永久",
            }
        ]
    )

    response = client.get(
        "/api/ban-list/22210347",
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )

    assert response.status_code == 200
    assert response.json()["bans"] == [
        {
            "block_id": 91,
            "uid": 9,
            "uname": "alias-user",
            "hour": -1,
            "reason": "manual reason",
            "created_at": "2026-07-10 19:00:00",
            "expires_at": "永久",
            "pending": False,
        }
    ]


# ---------------------------------------------------------------------------
# 7. WS /api/ws/rooms/{id}/banlist — connect → snapshot; on_ban → ban_added.
# ---------------------------------------------------------------------------


def test_ws_banlist_receives_snapshot_then_ban_added(
    client: TestClient,
    fake_bili_client: AsyncMock,
    fake_banlist_manager: _FakeBanListManager,
) -> None:
    """WS connect: receive ``snapshot``; trigger on_ban → receive ``ban_added``."""
    fake_bili_client.ban_user = AsyncMock(return_value=True)
    # Pre-populate the snapshot with an existing ban so we can tell the
    # snapshot apart from later deltas.
    fake_banlist_manager.bans = {1: {"uid": 1, "hour": 24}}

    with client.websocket_connect(
        f"/api/ws/rooms/22210347/banlist?token={settings.LOCAL_TOKEN}",
        headers={"Host": "localhost"},
    ) as ws:
        # 1. snapshot first
        snap = ws.receive_json()
        assert snap["event"] == "snapshot"
        assert len(snap["bans"]) == 1
        assert snap["bans"][0]["uid"] == 1

        # 2. trigger on_ban on the manager — the WS push callback should
        #    fire and deliver a ban_added frame.
        asyncio.run(
            fake_banlist_manager.on_ban(42, {"uid": 42, "hour": 1})
        )

        msg = ws.receive_json()
        assert msg["event"] == "ban_added"
        assert msg["ban"]["uid"] == 42

    # start() must have been called for the requested room.
    assert 22210347 in fake_banlist_manager.start_calls


def test_ws_banlist_disconnect_unsubscribes_no_callback_leak(
    client: TestClient,
    fake_bili_client: AsyncMock,
    fake_banlist_manager: _FakeBanListManager,
) -> None:
    """On WS close the push callback is removed from the manager's subscribers."""
    fake_bili_client.ban_user = AsyncMock(return_value=True)
    fake_banlist_manager.bans = {}

    with client.websocket_connect(
        f"/api/ws/rooms/22210347/banlist?token={settings.LOCAL_TOKEN}",
        headers={"Host": "localhost"},
    ) as ws:
        # Drain the snapshot (no entries, but the frame still arrives).
        snap = ws.receive_json()
        assert snap["event"] == "snapshot"
        assert snap["bans"] == []
        # Exactly one subscriber while the WS is open.
        assert len(fake_banlist_manager._subscribers) == 1

    # After disconnect — no subscribers left.
    assert fake_banlist_manager._subscribers == []


# ---------------------------------------------------------------------------
# 8. OpenAPI contains /api/ban and /api/ban-list/{room_id}.
# ---------------------------------------------------------------------------


def test_openapi_lists_ban_routes(client: TestClient) -> None:
    """``/openapi.json`` must enumerate the new ban endpoints."""
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/ban" in paths, "missing /api/ban in OpenAPI"
    assert "/api/ban-list/{room_id}" in paths, (
        "missing /api/ban-list/{room_id} in OpenAPI"
    )


def test_get_bili_client_refreshes_latest_settings_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import ban_routes

    monkeypatch.setattr(ban_routes, "_bili_client", None)
    fake = MagicMock(spec=BilibiliClient)
    fake.update_cookies = MagicMock(return_value=None)
    monkeypatch.setattr(ban_routes, "BilibiliClient", lambda: fake)

    resolved = ban_routes._get_bili_client()

    assert resolved is fake
    fake.update_cookies.assert_called_once_with(dict(settings.cookies))


async def test_stop_banlist_manager_cancels_and_clears_singleton(
    fake_banlist_manager: _FakeBanListManager,
) -> None:
    from app.api import ban_routes
    from app.room.banlist import get_banlist_manager, set_banlist_manager

    fake_banlist_manager._room_id = 22210347
    set_banlist_manager(fake_banlist_manager)  # type: ignore[arg-type]

    await ban_routes.stop_banlist_manager()

    assert fake_banlist_manager.stop_calls == 1
    assert ban_routes.banlist_manager is None
    assert get_banlist_manager() is None


# ---------------------------------------------------------------------------
# Adversarial — banlist_manager lifecycle: started on WS connect.
# ---------------------------------------------------------------------------


def test_ws_connect_starts_manager_for_the_requested_room(
    client: TestClient,
    fake_bili_client: AsyncMock,
    fake_banlist_manager: _FakeBanListManager,
) -> None:
    """On WS connect, ``manager.start(room_id, ...)`` is called for the room."""
    fake_bili_client.ban_user = AsyncMock(return_value=True)
    fake_banlist_manager.bans = {}

    with client.websocket_connect(
        f"/api/ws/rooms/22210347/banlist?token={settings.LOCAL_TOKEN}",
        headers={"Host": "localhost"},
    ) as ws:
        ws.receive_json()  # snapshot

    assert fake_banlist_manager.start_calls == [22210347]


def test_ws_connect_without_token_returns_401(client: TestClient) -> None:
    """Middleware blocks unauthenticated WS even on the new endpoint.

    Mirrors the T13 pattern: TestClient's ``websocket_connect`` does
    not raise on auth failure in the same way a real client would, so
    we drive the middleware with a plain ``GET`` against the WS path
    (the middleware code-path is identical).
    """
    response = client.get(
        "/api/ws/rooms/22210347/banlist",
        headers={"Host": "localhost"},
    )
    assert response.status_code == 401
