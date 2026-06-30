"""TDD tests for T13 — room routes + normalized WS bridge.

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

The ``LocalTokenMiddleware`` (T8) gates ``/api/*`` and ``/ws/*`` with
``Host: localhost`` + ``Authorization: Bearer <LOCAL_TOKEN>`` for HTTP
and ``?token=<LOCAL_TOKEN>`` for WebSockets. Tests set ``Host:
localhost`` and the token explicitly so the guard passes.

Adversarial scenarios exercised:

* WS disconnect → ``unregister_ws`` (no client left behind).
* Misleading ``send_json`` success vs. genuinely received JSON: assert
  the parsed dict, not the bare success.
* Dead WS removed on broadcast error (a raising ``send_json`` on one
  client does not break the others and the dead client is unregistered).
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.room.events import (
    DanmakuEvent,
    Medal,
    RoomStatusEvent,
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
def _isolated_bridge_singleton() -> Any:
    """Reset the module-level room bridge singleton between tests.

    ``set_room_bridge(None)`` is the documented seam used both in
    production (after ``stop``) and in tests (in setup + teardown).
    """
    from app.api import room_routes

    room_routes.set_room_bridge(None)
    yield
    room_routes.set_room_bridge(None)


@pytest.fixture
def fake_session_factory(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace ``_make_room_session`` with a builder that returns AsyncMocks.

    Returns a tuple ``(factory_calls, make_mock)``:

    * ``factory_calls`` is a list populated with every ``bili_client``
      the factory was invoked with — handy for asserting wiring.
    * ``make_mock(connect_return=...)`` builds a fresh
      ``AsyncMock(spec=RoomSession)`` with the right async return
      values and a unique callback registry.
    """

    factory_calls: list[Any] = []
    mocks: list[AsyncMock] = []

    def _build() -> AsyncMock:
        mock = AsyncMock(spec=RoomSession)
        mocks.append(mock)
        return mock

    async def factory(bili_client: Any) -> AsyncMock:
        factory_calls.append(bili_client)
        return _build()

    from app.api import room_routes

    monkeypatch.setattr(room_routes, "_make_room_session", factory)
    return type(
        "_Fakes",
        (),
        {"factory_calls": factory_calls, "mocks": mocks, "make": _build},
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
        *args: Any, **kwargs: Any
    ) -> None:  # pragma: no cover - trivial
        return None

    mock.add_callback = AsyncMock(side_effect=_pass_through)
    mock.remove_callback = AsyncMock(side_effect=_pass_through)
    mock.room_id = room_id
    mock.status = status
    return mock


@pytest.fixture
def mock_resolve_ok(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch ``_get_bili_client`` so ``resolve_room_id`` returns a dict."""
    from unittest.mock import AsyncMock

    payload: dict[str, Any] = {
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

    def _factory() -> Any:
        return bili

    monkeypatch.setattr(room_routes, "_get_bili_client", _factory)
    return bili


@pytest.fixture
def mock_resolve_none(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch ``_get_bili_client`` so ``resolve_room_id`` returns None."""
    from unittest.mock import AsyncMock

    bili = AsyncMock()
    bili.resolve_room_id = AsyncMock(return_value=None)

    from app.api import room_routes

    def _factory() -> Any:
        return bili

    monkeypatch.setattr(room_routes, "_get_bili_client", _factory)
    return bili


@pytest.fixture
def app() -> Any:
    """Build a fresh FastAPI app instance for these tests.

    Room routes have no lifespan side-effects (no HTTP client / no auth
    check here) so a plain ``create_app()`` is enough; we just need a
    stable singleton reset before/after each test.
    """
    from app.main import create_app

    return create_app()


@pytest.fixture
def client(app: FastAPI) -> Any:
    """Run the app under TestClient WITH the lifespan (T8 precedent)."""
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
    fake_session_factory: Any,
) -> None:
    """connect=True → 200 + singleton bridge is populated; ``connect`` was awaited."""
    from app.api import room_routes

    # Build the mock session up-front so we can inspect call counts.
    expected_mock = fake_session_factory.make()
    expected_mock.connect = AsyncMock(return_value=True)

    # Replace the factory's make() so the FIRST factory call returns OUR mock.
    original_factory = room_routes._make_room_session

    async def factory_with_mock(bili: Any) -> Any:
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

    bridge = room_routes.get_room_bridge()
    assert bridge is not None, "start must set the singleton bridge"
    assert expected_mock.connect.await_count == 1
    expected_mock.connect.assert_awaited_with(22210347)


# ---------------------------------------------------------------------------
# 4. POST /api/rooms/start (mock connect → False) → 400; bridge NOT set.
# ---------------------------------------------------------------------------


def test_start_returns_400_when_connect_fails(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_session_factory: Any,
) -> None:
    """connect=False → 400 and the singleton bridge stays None."""
    from app.api import room_routes

    expected_mock = fake_session_factory.make()
    expected_mock.connect = AsyncMock(return_value=False)

    original_factory = room_routes._make_room_session

    async def factory_with_mock(bili: Any) -> Any:
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
    bridge = room_routes.RoomBridge.__new__(room_routes.RoomBridge)
    bridge._room_session = session
    bridge._history = __import__("collections").deque(maxlen=100)
    bridge._clients = set()
    bridge._register_lock = asyncio.Lock()
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
    history: list[Any],
) -> Any:
    """Install a pre-loaded ``RoomBridge`` into the singleton for WS tests.

    Returns the installed :class:`RoomBridge` so callers can poke at
    its internals (e.g. trigger a broadcast via ``_on_event``).
    """
    from collections import deque

    from app.api import room_routes

    session = _make_mock_session(
        connect_return=True, room_id=active_room_id, status="connected"
    )
    bridge = room_routes.RoomBridge.__new__(room_routes.RoomBridge)
    bridge._room_session = session
    bridge._history = deque(history, maxlen=100)
    bridge._clients = set()
    bridge._register_lock = asyncio.Lock()
    room_routes.set_room_bridge(bridge)
    return bridge


def test_ws_receives_history_snapshot_then_live_events(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WS connect → replay every history event as JSON → live broadcast is JSON."""
    history: list[Any] = [
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
# Adversarial — WS disconnect → unregister_ws (no leak).
# ---------------------------------------------------------------------------


def test_ws_disconnect_unregisters_client(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On WS close the registry set is empty again (no client accumulates)."""
    history: list[Any] = [
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
    history: list[Any] = [
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

            async def send_json(self, _payload: Any) -> None:
                raise RuntimeError("send_json: connection reset")

        dead = _DeadWS()

        async def _drive() -> None:
            # Inject the dead peer directly into the registry — bypass
            # ``register_ws`` because that path tries the snapshot replay
            # first and would drop ``dead`` before the live-event path.
            bridge._clients.add(dead)
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
