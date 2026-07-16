"""Tests for room routes and the normalized WebSocket bridge.

Contract under test:

* ``GET  /api/rooms/resolve?input=<int>``     → 200 with B站-resolved data
                                                  or 404 when unresolvable.
* ``POST /api/rooms/start`` {room_id}         → 200 + bridge singleton set on
                                                  connect success; 400 on connect
                                                  failure.
* ``POST /api/rooms/stop``                    → 200 + bridge singleton cleared.
* ``GET  /api/rooms``                         → {current: room_id|None, status}.
* ``WS   /ws/rooms/{room_id}``                → accepts connection; if no active
                                                  room bridge for {room_id},
                                                  sends ``{type:"error",...}`` and
                                                  closes; otherwise registers
                                                  the WS, replays the recent
                                                  history (max 50 events as JSON),
                                                  then streams live normalized
                                                  events (``event.model_dump()``).

Mock strategy:

* ``app.api.room_routes.resolve_room_id`` is the import-by-name seam for
  the B站 resolution step. Tests patch it on the module so the route
  uses the fake without touching ``app.bilibili.client``.
* ``app.api.room_routes._make_room_session`` is the factory seam for
  constructing the underlying ``RoomSession``. Tests return a mock
  session that records ``connect``/``disconnect``/``add_callback`` calls
  instead of opening a real WebSocket.
* ``app.api.room_routes.get_room_bridge`` /
  ``app.api.room_routes.set_room_bridge`` are the singleton seam so each
  test can pin / clear the active bridge deterministically.

``LocalTokenMiddleware`` gates ``/api/*`` and ``/ws/*`` with
``Host: localhost`` + ``Authorization: Bearer <LOCAL_TOKEN>`` for HTTP
and ``?token=<LOCAL_TOKEN>`` for WebSockets. Tests set ``Host:
localhost`` and the token explicitly so the guard passes.

Adversarial scenarios exercised:

* WS disconnect → ``unregister_ws`` (no client left behind).
* Misleading ``send_json`` success vs. genuinely received JSON: assert
  the parsed dict, not the bare success.
* Dead WS removed on sender error (a raising ``send_json`` on one client
  does not break the others and the dead client is unregistered).
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.bilibili.client import BilibiliClient
from app.config import settings
from app.room.bridge import RoomBridge
from app.room.events import (
    BridgeEvent,
    DanmakuEvent,
    Medal,
    RoomStatusEvent,
    SuperChatDeleteEvent,
    SuperChatEvent,
)
from app.room.session import RoomSession

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _bearer(token: str) -> dict[str, str]:
    """Authorization header for the LOCAL_TOKEN bearer guard."""
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _isolated_bridge_singleton() -> Iterator[None]:
    """Reset the module-level room bridge singleton between tests.

    ``set_room_bridge(None)`` is the documented seam used both in
    production (after ``stop``) and in tests (in setup + teardown).
    """
    from app.api import room_routes

    room_routes.set_room_bridge(None)
    yield
    room_routes.set_room_bridge(None)


class _FakeFactory:
    """Bundle of state captured by the :func:`fake_session_factory` fixture.

    Holds the bookkeeping tests need to assert wiring without re-doing
    ``monkeypatch`` calls themselves.
    """

    factory_calls: list[BilibiliClient]
    mocks: list[AsyncMock]
    make: Callable[[], AsyncMock]
    bili_client: AsyncMock

    def __init__(
        self,
        factory_calls: list[BilibiliClient],
        mocks: list[AsyncMock],
        make_fn: Callable[[], AsyncMock],
        bili_client: AsyncMock,
    ) -> None:
        self.factory_calls = factory_calls
        self.mocks = mocks
        self.make = make_fn
        self.bili_client = bili_client


@pytest.fixture
def fake_session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> _FakeFactory:
    """Replace ``_make_room_session`` with a builder that returns AsyncMocks.

    Returns a :class:`_FakeFactory`:

    * ``factory_calls`` is a list populated with every
      ``bili_client`` the factory was invoked with — handy for
      asserting wiring.
    * ``make()`` builds a fresh ``AsyncMock(spec=RoomSession)`` with
      the right async return values and a unique callback registry.
    """

    factory_calls: list[BilibiliClient] = []
    mocks: list[AsyncMock] = []
    bili_client = AsyncMock(spec=BilibiliClient)
    bili_client.get_room_user_role = AsyncMock(return_value="viewer")

    def _build() -> AsyncMock:
        mock = AsyncMock(spec=RoomSession)
        mocks.append(mock)
        return mock

    async def factory(bili_client: BilibiliClient) -> AsyncMock:
        factory_calls.append(bili_client)
        return _build()

    from app.api import room_routes

    monkeypatch.setattr(room_routes, "_make_room_session", factory)
    monkeypatch.setattr(room_routes, "_get_bili_client", lambda: bili_client)
    return _FakeFactory(
        factory_calls=factory_calls,
        mocks=mocks,
        make_fn=_build,
        bili_client=bili_client,
    )


def _make_mock_session(
    *,
    connect_return: bool = True,
    disconnect_return: None = None,
    room_id: int | None = None,
    status: str = "disconnected",
) -> AsyncMock:
    """Build a single-purpose ``AsyncMock(spec=RoomSession)``.

    ``connect()`` returns ``connect_return``; ``disconnect()`` returns
    ``None``; ``add_callback`` / ``remove_callback`` are async no-ops
    that record the callback argument. ``room_id`` and ``status`` are
    set as plain attributes so the spec's instance attributes don't get
    replaced by auto-generated child mocks (which break ``!=`` and the
    WS route's room-id check).

    Returns the mock so individual tests can also wire ``side_effect``
    for the few cases that need a different behaviour.
    """
    mock = AsyncMock(spec=RoomSession)
    mock.connect = AsyncMock(return_value=connect_return)
    mock.disconnect = AsyncMock(return_value=disconnect_return)

    # add_callback / remove_callback must accept any callable and return
    # a coroutine — match the real signature.
    async def _pass_through(
        *args: object, **kwargs: object
    ) -> None:  # pragma: no cover - trivial
        return None

    mock.add_callback = AsyncMock(side_effect=_pass_through)
    mock.remove_callback = AsyncMock(side_effect=_pass_through)
    mock.room_id = room_id
    mock.status = status
    return mock


@pytest.fixture
def mock_resolve_ok(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Patch ``_get_bili_client`` so ``resolve_room_id`` returns a dict."""
    payload: dict[str, object] = {
        "room_id": 22210347,
        "short_id": 22210347,
        "uid": 12345,
        "title": "Test Room",
        "uname": "alice",
        "live_status": 1,
        "is_short_id": False,
    }

    bili = AsyncMock()
    bili.resolve_room_id = AsyncMock(return_value=dict(payload))

    from app.api import room_routes

    def _factory() -> AsyncMock:
        return bili

    monkeypatch.setattr(room_routes, "_get_bili_client", _factory)
    return bili


@pytest.fixture
def mock_resolve_none(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Patch ``_get_bili_client`` so ``resolve_room_id`` returns None."""
    bili = AsyncMock()
    bili.resolve_room_id = AsyncMock(return_value=None)

    from app.api import room_routes

    def _factory() -> AsyncMock:
        return bili

    monkeypatch.setattr(room_routes, "_get_bili_client", _factory)
    return bili


@pytest.fixture
def app() -> FastAPI:
    """Build a fresh FastAPI app instance for these tests.

    Room routes have no lifespan side-effects (no HTTP client / no auth
    check here) so a plain ``create_app()`` is enough; we just need a
    stable singleton reset before/after each test.
    """
    from app.main import create_app

    return create_app()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Run the app under TestClient with the lifespan enabled."""
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# 1. GET /api/rooms/resolve?input=22210347 → 200 with B站 data.
# ---------------------------------------------------------------------------


def test_resolve_returns_200_with_resolved_data(
    client: TestClient,
    mock_resolve_ok: None,
) -> None:
    """Happy path: resolve returns the B站 dict → 200 with the same payload."""
    response = client.get(
        "/api/rooms/resolve",
        params={"input": 22210347},
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["room_id"] == 22210347
    assert body["title"] == "Test Room"
    assert body["uid"] == 12345


def test_resolve_without_token_returns_401(
    client: TestClient,
    mock_resolve_ok: None,
) -> None:
    """Token guard must apply even on GET (defence-in-depth)."""
    response = client.get(
        "/api/rooms/resolve",
        params={"input": 22210347},
        headers={"Host": "localhost"},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# 2. GET /api/rooms/resolve?input=999 (resolve → None) → 404.
# ---------------------------------------------------------------------------


def test_resolve_returns_404_when_unresolvable(
    client: TestClient,
    mock_resolve_none: None,
) -> None:
    """``resolve_room_id`` returned ``None`` → 404."""
    response = client.get(
        "/api/rooms/resolve",
        params={"input": 999},
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 3. POST /api/rooms/start (mock RoomSession.connect → True) → 200; bridge set.
# ---------------------------------------------------------------------------


def test_start_returns_200_and_sets_bridge_when_connect_succeeds(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_session_factory: _FakeFactory,
) -> None:
    """connect=True → 200 + singleton bridge is populated; ``connect`` was awaited."""
    from app.api import room_routes

    # Build the mock session up-front so we can inspect call counts.
    expected_mock = fake_session_factory.make()
    expected_mock.connect = AsyncMock(return_value=True)

    # Replace the factory's make() so the FIRST factory call returns OUR mock.
    original_factory = room_routes._make_room_session

    async def factory_with_mock(bili: BilibiliClient) -> AsyncMock:
        await original_factory(bili)  # records the call
        return expected_mock

    monkeypatch.setattr(room_routes, "_make_room_session", factory_with_mock)

    response = client.post(
        "/api/rooms/start",
        json={"room_id": 22210347},
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["room_id"] == 22210347
    assert body["role"] == "viewer"

    bridge = room_routes.get_room_bridge()
    assert bridge is not None, "start must set the singleton bridge"
    assert expected_mock.connect.await_count == 1
    expected_mock.connect.assert_awaited_with(22210347)
    fake_session_factory.bili_client.get_room_user_role.assert_awaited_once_with(
        22210347
    )


def test_start_keeps_room_connected_when_role_lookup_fails(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_session_factory: _FakeFactory,
) -> None:
    from app.api import room_routes

    session = fake_session_factory.make()
    session.connect = AsyncMock(return_value=True)
    session.room_id = 1601605

    async def factory_with_session(_bili: BilibiliClient) -> AsyncMock:
        return session

    monkeypatch.setattr(room_routes, "_make_room_session", factory_with_session)
    fake_session_factory.bili_client.get_room_user_role.side_effect = OSError(
        "temporary read failure"
    )

    response = client.post(
        "/api/rooms/start",
        json={"room_id": 1601605},
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )

    assert response.status_code == 200
    assert response.json()["role"] == "unknown"
    assert room_routes.get_room_bridge() is not None


# ---------------------------------------------------------------------------
# 4. POST /api/rooms/start (mock connect → False) → 400; bridge NOT set.
# ---------------------------------------------------------------------------


def test_start_returns_400_when_connect_fails(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_session_factory: _FakeFactory,
) -> None:
    """connect=False → 400 and the singleton bridge stays None."""
    from app.api import room_routes

    expected_mock = fake_session_factory.make()
    expected_mock.connect = AsyncMock(return_value=False)

    original_factory = room_routes._make_room_session

    async def factory_with_mock(bili: BilibiliClient) -> AsyncMock:
        await original_factory(bili)
        return expected_mock

    monkeypatch.setattr(room_routes, "_make_room_session", factory_with_mock)

    response = client.post(
        "/api/rooms/start",
        json={"room_id": 22210347},
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 400
    assert room_routes.get_room_bridge() is None, (
        "failed connect must NOT install a bridge singleton"
    )


def test_start_auth_expired_returns_stable_401_and_transitions_session(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_session_factory: _FakeFactory,
) -> None:
    from app.api import room_routes
    from app.auth import session as auth_session_module
    from app.bilibili.exceptions import AuthExpiredError

    session = fake_session_factory.make()
    session.connect = AsyncMock(side_effect=AuthExpiredError("expired"))

    async def factory_with_session(_bili: BilibiliClient) -> AsyncMock:
        return session

    handle_expired = AsyncMock(return_value=None)
    monkeypatch.setattr(room_routes, "_make_room_session", factory_with_session)
    monkeypatch.setattr(
        auth_session_module.auth_session,
        "handle_auth_expired",
        handle_expired,
    )

    response = client.post(
        "/api/rooms/start",
        json={"room_id": 1601605},
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "BILI_AUTH_EXPIRED"
    handle_expired.assert_awaited_once()
    assert room_routes.get_room_bridge() is None


# ---------------------------------------------------------------------------
# 5. POST /api/rooms/stop → 200; bridge cleared.
# ---------------------------------------------------------------------------


def test_stop_returns_200_and_clears_bridge(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-install a fake bridge; POST /stop disconnects + clears + 200."""
    from app.api import room_routes

    # Pre-install a bridge wrapping a mock session.
    session = _make_mock_session(
        connect_return=True, room_id=22210347, status="connected"
    )
    bridge = room_routes.RoomBridge(session)
    room_routes.set_room_bridge(bridge)

    response = client.post(
        "/api/rooms/stop",
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 200
    assert room_routes.get_room_bridge() is None
    session.disconnect.assert_awaited_once()


def test_stop_is_noop_when_no_bridge_returns_200(
    client: TestClient,
) -> None:
    """Calling /stop with no active bridge is a no-op success (idempotent)."""
    response = client.post(
        "/api/rooms/stop",
        headers={"Host": "localhost", **_bearer(settings.LOCAL_TOKEN)},
    )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# 6. WS /ws/rooms/{id}: receives history snapshot then live events.
# ---------------------------------------------------------------------------


def _install_bridge_with_history(
    monkeypatch: pytest.MonkeyPatch,
    *,
    active_room_id: int,
    history: list[BridgeEvent],
) -> RoomBridge:
    """Install a pre-loaded ``RoomBridge`` into the singleton for WS tests.

    Returns the installed :class:`RoomBridge` so callers can poke at
    its internals (e.g. trigger a broadcast via ``_on_event``).
    """
    from collections import deque

    from app.api import room_routes

    session = _make_mock_session(
        connect_return=True, room_id=active_room_id, status="connected"
    )
    bridge = room_routes.RoomBridge(session)
    bridge._history = deque(history, maxlen=100)
    room_routes.set_room_bridge(bridge)
    return bridge


def test_ws_receives_history_snapshot_then_live_events(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WS connect → replay every history event as JSON → live broadcast is JSON."""
    history: list[BridgeEvent] = [
        DanmakuEvent(
            type="danmaku",
            uid=1,
            uname="alice",
            text="hello",
            ts=1700000000,
            guard_level=0,
            medal=None,
        ),
        SuperChatEvent(
            type="sc",
            uid=2,
            uname="bob",
            text="sc!",
            price=30,
            ts=1700000001,
        ),
    ]
    bridge = _install_bridge_with_history(
        monkeypatch, active_room_id=22210347, history=history
    )

    with client.websocket_connect(
        "/api/ws/rooms/22210347",
        headers={
            "Host": "localhost",
            **_bearer(settings.LOCAL_TOKEN),
        },
    ) as ws:
        # Snapshot first.
        snap_a = ws.receive_json()
        assert snap_a["type"] == "danmaku"
        assert snap_a["text"] == "hello"

        snap_b = ws.receive_json()
        assert snap_b["type"] == "sc"
        assert snap_b["text"] == "sc!"

        # Live broadcast.
        live_event = RoomStatusEvent(type="room_status", status="connected")
        asyncio.run(bridge._on_event(live_event))

        live = ws.receive_json()
        assert live["type"] == "room_status"
        assert live["status"] == "connected"


def test_active_super_chat_replays_after_chat_history_eviction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long-lived SC is replayed even after 100 ordinary chat events."""
    history: list[BridgeEvent] = [
        DanmakuEvent(
            type="danmaku",
            uid=i,
            uname=f"u{i}",
            text=f"m{i}",
            ts=1700000000 + i,
            guard_level=0,
            medal=None,
        )
        for i in range(100)
    ]
    bridge = _install_bridge_with_history(
        monkeypatch, active_room_id=22210347, history=history
    )
    active_sc = SuperChatEvent(
        type="sc",
        id="paid-1",
        uid=8,
        uname="supporter",
        text="still pinned",
        price=2000,
        ts=int(time.time()),
        end_ts=int(time.time()) + 7200,
        duration=7200,
    )
    bridge._active_super_chats = {active_sc.id: active_sc}

    ws = MagicMock()
    ws.send_json = AsyncMock()

    async def _register_and_flush() -> None:
        await bridge.register_ws(ws)
        queue = bridge._client_queues[ws]
        await asyncio.wait_for(queue.join(), timeout=1.0)
        await bridge.unregister_ws(ws)

    asyncio.run(_register_and_flush())

    first_payload = ws.send_json.await_args_list[0].args[0]
    assert first_payload["type"] == "sc"
    assert first_payload["id"] == "paid-1"
    # Active SC + the capped 50 ordinary history events.
    assert ws.send_json.await_count == 51


def test_super_chat_delete_removes_bridge_active_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _install_bridge_with_history(
        monkeypatch, active_room_id=22210347, history=[]
    )
    sc = SuperChatEvent(
        type="sc",
        id="paid-delete",
        uid=9,
        uname="user",
        text="remove me",
        price=30,
        ts=int(time.time()),
        end_ts=int(time.time()) + 60,
        duration=60,
    )
    asyncio.run(bridge._on_event(sc))
    assert [item.id for item in bridge.active_super_chats] == ["paid-delete"]

    asyncio.run(
        bridge._on_event(
            SuperChatDeleteEvent(type="sc_delete", ids=["paid-delete"])
        )
    )
    assert bridge.active_super_chats == []


def test_ws_sends_only_normalized_event_dump_no_raw_keys(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec: never forward raw B站 cmd/info dicts — only ``model_dump()`` shape."""
    bridge = _install_bridge_with_history(monkeypatch, active_room_id=22210347, history=[])

    with client.websocket_connect(
        "/api/ws/rooms/22210347",
        headers={
            "Host": "localhost",
            **_bearer(settings.LOCAL_TOKEN),
        },
    ) as ws:
        event = DanmakuEvent(
            type="danmaku",
            uid=7,
            uname="carol",
            text="no-raw",
            ts=1700000099,
            guard_level=0,
            medal=Medal(name="m", level=1),
        )
        asyncio.run(bridge._on_event(event))
        payload = ws.receive_json()

    # The forbidden raw keys must never appear.
    for forbidden in ("cmd", "info", "dm_v2", "data"):
        assert forbidden not in payload, (
            f"raw B站 key {forbidden!r} leaked into WS payload: {payload!r}"
        )
    # model_dump() shape — top-level fields are exactly the typed fields.
    assert set(payload.keys()) == {
        "type",
        "uid",
        "uname",
        "text",
        "ts",
        "guard_level",
        "medal",
    }


# ---------------------------------------------------------------------------
# 7. WS /ws/rooms/{id} when no room → {type:"error"} + close.
# ---------------------------------------------------------------------------


def test_ws_with_no_active_bridge_sends_error_and_closes(
    client: TestClient,
) -> None:
    """No bridge installed → server sends an error JSON and closes."""
    with client.websocket_connect(
        "/api/ws/rooms/22210347",
        headers={
            "Host": "localhost",
            **_bearer(settings.LOCAL_TOKEN),
        },
    ) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "房间未启动" in msg["message"]
        # After the server-side close frame, ``receive_json`` raises.
        with pytest.raises(Exception):
            ws.receive_json()


def test_ws_with_mismatched_room_id_sends_error(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WS asks for room 1, bridge is on room 2 → error + close (no event leak)."""
    _install_bridge_with_history(monkeypatch, active_room_id=22210347, history=[])

    with client.websocket_connect(
        "/api/ws/rooms/999",
        headers={
            "Host": "localhost",
            **_bearer(settings.LOCAL_TOKEN),
        },
    ) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"
        with pytest.raises(Exception):
            ws.receive_json()


# ---------------------------------------------------------------------------
# 8. OpenAPI: all four /api/rooms/* routes are visible.
# ---------------------------------------------------------------------------


def test_openapi_lists_all_room_routes(client: TestClient) -> None:
    """``/openapi.json`` must enumerate every room endpoint this PR adds."""
    paths = client.get("/openapi.json").json()["paths"]
    for path in (
        "/api/rooms/resolve",
        "/api/rooms/start",
        "/api/rooms/stop",
        "/api/rooms",
    ):
        assert path in paths, f"missing path in OpenAPI: {path}"


# ---------------------------------------------------------------------------
# 9. WS auth — /api/ws/* must accept ?token=<LOCAL_TOKEN>.
# ---------------------------------------------------------------------------
#
# The middleware code-path is identical for HTTP and WS; we drive it
# with plain ``GET`` requests for test simplicity (the route is WS-only,
# so anything past the guard returns 404).


def test_ws_query_token_authenticates_under_api_prefix(
    client: TestClient,
) -> None:
    """``?token=<LOCAL_TOKEN>`` on ``/api/ws/*`` passes the guard."""
    response = client.get(
        f"/api/ws/rooms/22210347?token={settings.LOCAL_TOKEN}",
        headers={"Host": "localhost"},
    )
    assert response.status_code != 401


def test_ws_without_token_under_api_prefix_returns_401(
    client: TestClient,
) -> None:
    """No header, no query token → guard rejects with 401."""
    response = client.get(
        "/api/ws/rooms/22210347",
        headers={"Host": "localhost"},
    )
    assert response.status_code == 401


def test_ws_with_wrong_query_token_under_api_prefix_returns_401(
    client: TestClient,
) -> None:
    """Wrong ``?token=`` value → guard rejects with 401."""
    response = client.get(
        "/api/ws/rooms/22210347?token=definitely-not-the-right-token",
        headers={"Host": "localhost"},
    )
    assert response.status_code == 401


def test_bare_ws_path_still_accepts_query_token(
    client: TestClient,
) -> None:
    """The pre-existing ``/ws/*`` branch must keep working after the fix."""
    response = client.get(
        f"/ws/rooms/22210347?token={settings.LOCAL_TOKEN}",
        headers={"Host": "localhost"},
    )
    assert response.status_code != 401, (
        "middleware must accept ?token=<LOCAL_TOKEN> for /ws/* paths"
    )


def test_bare_ws_path_without_token_returns_401(
    client: TestClient,
) -> None:
    """No token on ``/ws/*`` → guard rejects with 401."""
    response = client.get(
        "/ws/rooms/22210347",
        headers={"Host": "localhost"},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# _get_bili_client — refresh the singleton's cookie jar from settings
# ---------------------------------------------------------------------------
#
# Bug fix: the room-routes singleton ``_get_bili_client`` may have been
# created BEFORE a QR / manual login mutated ``settings.cookies`` in
# place. Each access must refresh the jar from current settings so the
# route's downstream B站 calls (e.g. ``resolve_room_id``) carry the
# freshly-captured credentials.


def test_get_bili_client_refreshes_cookies_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First access to ``_get_bili_client`` must call ``update_cookies``
    with the current ``settings.cookies`` snapshot, so the long-lived
    client carries the freshly-captured credentials even if it was
    constructed earlier in the process (e.g. at import time with an
    empty ``.env``).
    """
    from app.api import room_routes
    from app.config import settings as real_settings

    room_routes._bili_client = None  # reset singleton

    fake_client = MagicMock(spec=["update_cookies", "resolve_room_id"])
    fake_client.update_cookies = MagicMock(return_value=None)

    bili_ctor_calls: list[dict[str, str] | None] = []

    def _fake_bili_client(*, cookies: dict[str, str] | None = None, **_kwargs: object) -> MagicMock:
        bili_ctor_calls.append(cookies)
        return fake_client

    monkeypatch.setattr(room_routes, "BilibiliClient", _fake_bili_client)

    resolved = room_routes._get_bili_client()

    assert resolved is fake_client
    fake_client.update_cookies.assert_called_once_with(dict(real_settings.cookies))
    # Subsequent access reuses the singleton and does NOT re-call the ctor.
    bili_ctor_calls.clear()
    resolved2 = room_routes._get_bili_client()
    assert resolved2 is fake_client
    assert bili_ctor_calls == []


def test_get_bili_client_subsequent_calls_also_refresh_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every ``_get_bili_client()`` access must refresh the jar — cookies
    may have been mutated in place between calls (e.g. after the QR
    poll handler wrote the fresh credentials)."""
    from app.api import room_routes

    room_routes._bili_client = None

    fake_client = MagicMock(spec=["update_cookies"])
    fake_client.update_cookies = MagicMock(return_value=None)

    def _ctor(**_kwargs: object) -> MagicMock:
        return fake_client

    monkeypatch.setattr(room_routes, "BilibiliClient", _ctor)

    room_routes._get_bili_client()
    room_routes._get_bili_client()
    room_routes._get_bili_client()

    assert fake_client.update_cookies.call_count == 3


# ---------------------------------------------------------------------------
# Adversarial — WS disconnect → unregister_ws (no leak).
# ---------------------------------------------------------------------------


def test_ws_disconnect_unregisters_client(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On WS close the registry set is empty again (no client accumulates)."""
    history: list[BridgeEvent] = [
        DanmakuEvent(
            type="danmaku",
            uid=1,
            uname="alice",
            text="seed",
            ts=1700000000,
            guard_level=0,
            medal=None,
        ),
    ]
    bridge = _install_bridge_with_history(
        monkeypatch, active_room_id=22210347, history=history
    )
    assert bridge._clients == set()

    with client.websocket_connect(
        "/api/ws/rooms/22210347",
        headers={
            "Host": "localhost",
            **_bearer(settings.LOCAL_TOKEN),
        },
    ) as ws:
        # Drain the snapshot — one event in the history.
        snap = ws.receive_json()
        assert snap["text"] == "seed"
        # At this point the route has registered the WS.
        assert len(bridge._clients) == 1

    # Context manager exit closes the WS → unregister must have run.
    assert bridge._clients == set()


# ---------------------------------------------------------------------------
# Adversarial — dead WS removed on broadcast error.
# ---------------------------------------------------------------------------


def test_broadcast_isolates_dead_ws_errors_and_unregisters_it(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``send_json`` raises on one client, the live ones still receive
    the event AND the broken client is removed from the registry.
    """
    history: list[BridgeEvent] = [
        DanmakuEvent(
            type="danmaku",
            uid=1,
            uname="alice",
            text="bootstrap",
            ts=1700000000,
            guard_level=0,
            medal=None,
        ),
    ]
    bridge = _install_bridge_with_history(
        monkeypatch, active_room_id=22210347, history=history
    )

    # Open WS #1 — the "good" one — through the normal flow.
    with client.websocket_connect(
        "/api/ws/rooms/22210347",
        headers={
            "Host": "localhost",
            **_bearer(settings.LOCAL_TOKEN),
        },
    ) as good_ws:
        # Drain the snapshot (1 event).
        snap = good_ws.receive_json()
        assert snap["text"] == "bootstrap"

        # Build a "dead" client that raises on send_json, register it
        # directly via the bridge internals, and broadcast.
        from starlette.websockets import WebSocketState

        class _DeadWS:
            """Minimal duck-typed WebSocket stand-in that always fails."""

            client_state = WebSocketState.CONNECTED

            async def send_json(self, _payload: object) -> None:
                raise RuntimeError("send_json: connection reset")

        dead = _DeadWS()

        async def _drive() -> None:
            # Inject the dead peer directly into the registry — bypass
            # ``register_ws`` because that path tries the snapshot replay
            # first and would drop ``dead`` before the live-event path.
            bridge._clients.add(dead)  # type: ignore[arg-type]
            assert dead in bridge._clients
            event = DanmakuEvent(
                type="danmaku",
                uid=2,
                uname="bob",
                text="live-1",
                ts=1700000099,
                guard_level=0,
                medal=None,
            )
            await bridge._on_event(event)

        asyncio.run(_drive())

        # The dead client must have been pruned during the broadcast.
        assert dead not in bridge._clients

        # The good client received the live event despite the dead raising.
        payload = good_ws.receive_json()
        assert payload["type"] == "danmaku"
        assert payload["text"] == "live-1"
