"""Contract tests for the B站 ban-list HTTP API (T20) and
:class:`~app.room.banlist.BanListManager` integration with synthetic
fetcher payloads.

These tests PIN the shape-and-code mapping from B站 ban-related
endpoints to typed Python behavior. They are SYNTHETIC — constructed
inline based on ccShield's known B站 field shapes
(``ccShield/app/core/bili_client.py`` lines 225-368 for ban / unban /
``GetSilentUserList``).

No real Cookie / network / captured fixtures are needed at this stage.
Real-fixture capture is deferred to the Wave 2 gate, where the
synthetic shapes here will be cross-validated against actual B站
captures. If B站 changes any code or field shape, the affected
contract test fails — prompting a re-capture / contract revision.

Scenarios covered
-----------------

  1. ``ban_user`` code=0           → returns ``True`` (success).
  2. ``ban_user`` code=-101        → raises ``AuthExpiredError``.
  3. ``ban_user`` code=-403        → raises ``PermissionDeniedError``.
  4. ``ban_user`` code=-509        → raises ``RateLimitedError``.
  5. ``ban_user`` code=other       → raises bare ``BiliApiError``.
  6. ``unban_user`` code=0         → returns ``True``.
  7. ``unban_user`` code=-101      → raises ``AuthExpiredError``.
  8. ``unban_user`` code=-403      → raises ``PermissionDeniedError`` (symmetry).
  9. ``get_ban_list`` single page  → returns the flat list of entries.
 10. ``get_ban_list`` multi-page   → merges entries across pages.
 11. ``get_ban_list`` empty        → returns ``[]`` (not ``None``).
 12. ``get_ban_list`` code=-101    → propagates ``AuthExpiredError``
      (errors must not be silently dropped to ``[]``).
 13. ``BanListManager.start``        → fetches via synthetic mock, broadcasts
      ``{event: "snapshot", bans: [...]}`` to every registered subscriber.
 14. ``BanListManager.on_ban``       → inserts uid + broadcasts ``ban_added``.
 15. ``BanListManager.on_unban``     → removes uid + broadcasts ``ban_removed``.
 16. ``BanListManager._reconcile``   → detects a NEW uid appearing in the
      next synthetic fetch and broadcasts ``ban_added``.
 17. Error-mapping contract          → a ``ban_user`` / manager-start
      fixture returning code=-403 surfaces ``PermissionDeniedError``,
      never silently returns ``True`` or ``[]``.

Reference shapes (ccShield ``app/core/bili_client.py``):

* ``AddSilentUser`` (ban) — envelope ``{"code": 0, "message": "ok"}``;
  failure carries ``{"code": -101 | -403 | -509 | other, "message": ...}``.
* ``del_room_block_user`` (unban) — same envelope contract.
* ``GetSilentUserList`` (read) — ``{"code": 0,
    "data": {"data": [{id, uid, uname, ...}], "total": N, "total_page": M}}``.
  Note the page-wrapped ``data.data``; the client flattens this.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import TypeVar, cast

import httpx
import pytest

from app.bilibili.client import BilibiliClient
from app.bilibili.exceptions import (
    AuthExpiredError,
    BiliApiError,
    PermissionDeniedError,
    RateLimitedError,
)
from app.bilibili.wbi import WbiSigner
from app.room.banlist import (
    BanListCallback,
    BanListManager,
    BanListMessage,
)

# ---------------------------------------------------------------------------
# Synthetic B站 response fixtures (inline dicts based on ccShield shapes)
# ---------------------------------------------------------------------------

# Write-API envelopes — ban / unban responses.
BAN_OK: dict[str, object] = {"code": 0, "message": "ok"}
BAN_AUTH_EXPIRED: dict[str, object] = {"code": -101, "message": "账号未登录"}
BAN_PERMISSION_DENIED: dict[str, object] = {"code": -403, "message": "无权限"}
BAN_RATE_LIMITED: dict[str, object] = {"code": -509, "message": "风控"}
BAN_UNKNOWN_ERROR: dict[str, object] = {"code": -500, "message": "server boom"}


def _ban_entry(uid: int, block_id: int) -> dict[str, object]:
    """Single ban-list item (ccShield bili_client.py ban-list shape).

    ``uid`` is the banned user; ``id`` is the ``block_id`` used by
    ``del_room_block_user``. Extra fields (``uname``/``face``/``room_id``)
    mirror the canonical ccShield response — kept here so a contract
    change in those fields can be diffed at the Wave 2 gate.
    """
    return {
        "id": block_id,
        "uid": uid,
        "uname": f"user{uid}",
        "face": "",
        "room_id": 22210347,
    }


def _single_page_payload(entries: list[dict[str, object]]) -> dict[str, object]:
    """B站 single-page ``GetSilentUserList`` envelope.

    ``data.data`` is the per-page array; ``data.total`` and
    ``data.total_page`` are the pagination metadata.
    """
    return {
        "code": 0,
        "message": "0",
        "data": {
            "data": entries,
            "total": len(entries),
            "total_page": 1,
        },
    }


def _multi_page_payload_for_page(
    page: int, total: int, total_page: int
) -> dict[str, object]:
    """A page of a multi-page ``GetSilentUserList`` response.

    Each page holds 2 entries. Page ``n`` carries uids ``100 + 2*(n-1)``
    and ``100 + 2*(n-1) + 1`` so test assertions are deterministic
    regardless of the request order.
    """
    base = (page - 1) * 2
    items: list[dict[str, object]] = [
        _ban_entry(uid=100 + base + i, block_id=200 + base + i)
        for i in range(2)
    ]
    return {
        "code": 0,
        "message": "0",
        "data": {
            "data": items,
            "total": total,
            "total_page": total_page,
        },
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> BilibiliClient:
    """Wrap an httpx mock handler in a real ``BilibiliClient``.

    Mirrors the seam used by ``test_client.py``: a fresh
    :class:`~app.bilibili.wbi.WbiSigner` keeps per-test WBI cache
    state isolated (the module-level ``wbi_signer`` would leak between
    tests otherwise). ``csrf_token`` is a non-empty sentinel so we can
    assert its presence in form bodies.
    """
    transport = httpx.MockTransport(handler)
    inner = httpx.AsyncClient(transport=transport)
    return BilibiliClient(
        client=inner,
        cookies={},
        csrf_token="mock-csrf-token",
        signer=WbiSigner(),
    )


_T = TypeVar("_T")


def _run(coro: Awaitable[_T]) -> _T:
    """Run an async coroutine from a sync test (one-shot ``asyncio.run``)."""
    return asyncio.run(cast("Coroutine[None, None, _T]", coro))


def _static_handler(body: dict[str, object]) -> Callable[[httpx.Request], httpx.Response]:
    """Build an httpx mock handler that always responds with ``body``."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    return handler


def _always_running() -> bool:
    """Static ``is_running`` callback: room is always alive."""
    return True


# Patch asyncio.sleep in app.room.banlist (NOT in this module) so the
# reconcile loop yields immediately and fires deterministically. A real
# ``sleep(0)`` (rather than a bare ``AsyncMock``) is required — the mock
# would resolve instantly but NOT actually yield control, starving the
# background reconcile task.
_real_sleep = asyncio.sleep


async def _yield_sleep(_delay: float) -> None:
    """Patched ``asyncio.sleep``: ignore delay, yield control once."""
    await _real_sleep(0)


@pytest.fixture(autouse=True)
def _patch_banlist_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-test monkeypatch: fast reconcile loop in ``app.room.banlist``.

    See ``tests/test_banlist.py`` for the same pattern. Sync tests in
    this file that exercise ``BilibiliClient`` directly are unaffected
    (they don't import ``app.room.banlist``).
    """
    monkeypatch.setattr("app.room.banlist.asyncio.sleep", _yield_sleep)


async def _wait_for(predicate: Callable[[], bool], *, max_yields: int = 200) -> bool:
    """Yield to the loop until ``predicate()`` is truthy or we exhaust."""
    for _ in range(max_yields):
        if predicate():
            return True
        await asyncio.sleep(0)
    return predicate()


class _PageQueueHandler:
    """MockTransport handler returning a queue of synthetic ban-list pages.

    Records every request in ``calls`` so tests can inspect the path /
    form-body of every call. When the queue is empty, returns a
    legitimate empty page so the ban-list paginator exits cleanly
    without spurious errors.
    """

    def __init__(self, *payloads: dict[str, object]) -> None:
        self.queue: list[dict[str, object]] = list(payloads)
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if not self.queue:
            return httpx.Response(200, json=_single_page_payload([]))
        body = self.queue.pop(0)
        return httpx.Response(200, json=body)


def _make_manager(
    *payloads: dict[str, object], interval: float = 0.01
) -> tuple[BanListManager, _PageQueueHandler]:
    """Build a ``BanListManager`` wired to a real ``BilibiliClient``
    whose ``get_ban_list`` returns queued synthetic pages.

    Returns the manager and the queue handler so the test can introspect
    request counts and bodies.
    """
    handler = _PageQueueHandler(*payloads)
    client = _make_client(handler)
    mgr = BanListManager(bili_client=client, _reconcile_interval=interval)
    return mgr, handler


def _capture_cb(received: list[BanListMessage]) -> BanListCallback:
    """Return an async callback that appends every message to ``received``.

    Returns a :data:`~app.room.banlist.BanListCallback` so the result
    typechecks against ``BanListManager.subscribe`` directly.
    """

    async def cb(msg: BanListMessage) -> None:
        received.append(msg)

    return cb


# ===========================================================================
# BilibiliClient.ban_user contract tests
# ===========================================================================


def test_contract_ban_user_code_zero_returns_true() -> None:
    """``ban_user`` with code=0 MUST return ``True`` and POST to
    ``/xlive/web-ucenter/v1/banned/AddSilentUser`` with the expected
    form fields (csrf, room_id, tuid, hour, msg).
    """
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=BAN_OK)

    client = _make_client(handler)
    result = _run(
        client.ban_user(room_id=22210347, uid=12345, hour=1, msg="stop")
    )

    assert result is True
    assert len(captured) == 1
    path = captured[0].url.path
    assert path.endswith("/xlive/web-ucenter/v1/banned/AddSilentUser")

    body = captured[0].content.decode()
    # Form fields the ccShield client contract relies on.
    assert "room_id=22210347" in body
    assert "tuid=12345" in body
    assert "hour=1" in body
    assert "msg=stop" in body
    assert "csrf=mock-csrf-token" in body


def test_contract_ban_user_minus_101_raises_auth_expired() -> None:
    """code=-101 → ``AuthExpiredError`` (cookie expired)."""
    handler = _static_handler(BAN_AUTH_EXPIRED)
    client = _make_client(handler)
    with pytest.raises(AuthExpiredError) as excinfo:
        _run(client.ban_user(room_id=22210347, uid=1, hour=1))
    # Contract: code MUST be -101 (the typed exception carries it).
    assert excinfo.value.code == -101
    assert excinfo.value.message == "账号未登录"


def test_contract_ban_user_minus_403_raises_permission_denied() -> None:
    """code=-403 → ``PermissionDeniedError`` (caller is not a mod)."""
    handler = _static_handler(BAN_PERMISSION_DENIED)
    client = _make_client(handler)
    with pytest.raises(PermissionDeniedError) as excinfo:
        _run(client.ban_user(room_id=22210347, uid=1, hour=1))
    assert excinfo.value.code == -403
    assert excinfo.value.message == "无权限"


def test_contract_ban_user_minus_509_raises_rate_limited() -> None:
    """code=-509 → ``RateLimitedError``."""
    handler = _static_handler(BAN_RATE_LIMITED)
    client = _make_client(handler)
    with pytest.raises(RateLimitedError) as excinfo:
        _run(client.ban_user(room_id=22210347, uid=1, hour=1))
    assert excinfo.value.code == -509


def test_contract_ban_user_unknown_code_raises_base_bili_api_error() -> None:
    """Any other non-zero code → bare ``BiliApiError`` (fallback).

    Crucially: NOT one of the typed subclasses — the test verifies
    ``type(err) is BiliApiError`` so a future code-mapping change that
    accidentally matches a different branch breaks here.
    """
    handler = _static_handler(BAN_UNKNOWN_ERROR)
    client = _make_client(handler)
    with pytest.raises(BiliApiError) as excinfo:
        _run(client.ban_user(room_id=22210347, uid=1, hour=1))
    assert excinfo.value.code == -500
    assert type(excinfo.value) is BiliApiError


# ===========================================================================
# BilibiliClient.unban_user contract tests
# ===========================================================================


def test_contract_unban_user_code_zero_returns_true() -> None:
    """``unban_user`` with code=0 → ``True`` and POSTs to
    ``/banned_service/v1/Silent/del_room_block_user`` with
    ``roomid``/``id``/``csrf``.
    """
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=BAN_OK)

    client = _make_client(handler)
    result = _run(client.unban_user(room_id=22210347, block_id=42))

    assert result is True
    assert len(captured) == 1
    path = captured[0].url.path
    assert path.endswith("/banned_service/v1/Silent/del_room_block_user")

    body = captured[0].content.decode()
    assert "roomid=22210347" in body
    assert "id=42" in body
    assert "csrf=mock-csrf-token" in body


def test_contract_unban_user_minus_101_raises_auth_expired() -> None:
    """code=-101 → ``AuthExpiredError`` (symmetric with ban_user)."""
    handler = _static_handler(BAN_AUTH_EXPIRED)
    client = _make_client(handler)
    with pytest.raises(AuthExpiredError):
        _run(client.unban_user(room_id=22210347, block_id=42))


def test_contract_unban_user_minus_403_raises_permission_denied() -> None:
    """code=-403 → ``PermissionDeniedError`` (symmetric with ban_user).

    The spec only mandates -101 for unban, but the symmetric mapping
    pins that ANY write endpoint with -403 surfaces the same typed
    error — otherwise the manager would treat unban-403 as
    distinguishable from ban-403.
    """
    handler = _static_handler(BAN_PERMISSION_DENIED)
    client = _make_client(handler)
    with pytest.raises(PermissionDeniedError):
        _run(client.unban_user(room_id=22210347, block_id=42))


# ===========================================================================
# BilibiliClient.get_ban_list contract tests
# ===========================================================================


def test_contract_get_ban_list_single_page_returns_all_entries() -> None:
    """Single-page ``GetSilentUserList`` is flattened to a list of ban
    entries; ``data.data`` page-wrapped shape is unwrapped.

    With ``total_page=1`` exactly one HTTP call is expected.
    """
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json=_single_page_payload(
                [
                    _ban_entry(uid=1, block_id=101),
                    _ban_entry(uid=2, block_id=102),
                ]
            ),
        )

    client = _make_client(handler)
    bans = _run(client.get_ban_list(22210347))

    assert len(bans) == 2
    assert bans[0]["uid"] == 1
    assert bans[0]["id"] == 101
    assert bans[1]["uid"] == 2
    assert bans[1]["id"] == 102
    # Single page ⇒ exactly one HTTP call.
    assert len(captured) == 1
    # The form body uses ``ps=1`` per B站 contract.
    assert "ps=1" in captured[0].content.decode()


def test_contract_get_ban_list_multi_page_merges_all_pages() -> None:
    """Multi-page ``GetSilentUserList`` MUST paginate and merge across
    pages until ``total_page`` is exhausted.

    Page 1: uids 100, 101. Page 2: uids 102, 103. Final: 4 entries.
    Each page carries the same ``total=4, total_page=2``.
    """
    pages: list[dict[str, object]] = [
        _multi_page_payload_for_page(page=1, total=4, total_page=2),
        _multi_page_payload_for_page(page=2, total=4, total_page=2),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        ps_str = body.split("ps=")[1].split("&")[0]
        ps = int(ps_str)
        return httpx.Response(200, json=pages[ps - 1])

    client = _make_client(handler)
    bans = _run(client.get_ban_list(22210347))

    assert len(bans) == 4
    assert sorted(b["uid"] for b in bans) == [100, 101, 102, 103]
    assert sorted(b["id"] for b in bans) == [200, 201, 202, 203]


def test_contract_get_ban_list_empty_returns_empty_list() -> None:
    """No banned users → ``[]`` (an empty list, never ``None``).

    Guards against a future regression that returns ``None`` for an
    empty page (downstream code uses ``for entry in bans: ...`` which
    would crash on ``None``).
    """
    handler = _static_handler(_single_page_payload([]))
    client = _make_client(handler)
    result = _run(client.get_ban_list(22210347))

    assert result == []
    assert isinstance(result, list)


def test_contract_get_ban_list_auth_expired_propagates() -> None:
    """A ban-list page with code=-101 must propagate ``AuthExpiredError``
    (NOT silently return ``[]``).

    This pins the error contract: a -403 / -509 / -101 on the FIRST page
    is a hard authentication/permission failure and must not be masked
    as "empty room".
    """
    handler = _static_handler(BAN_AUTH_EXPIRED)
    client = _make_client(handler)
    with pytest.raises(AuthExpiredError):
        _run(client.get_ban_list(22210347))


# ===========================================================================
# BanListManager integration: snapshot / delta / reconcile
# ===========================================================================


async def test_contract_manager_start_broadcasts_snapshot() -> None:
    """``manager.start`` MUST fetch the synthetic ban-list, populate
    ``_bans``, and broadcast ``{event: "snapshot", bans: [...]}`` to
    every registered subscriber.

    The mock fetcher returns a single page with one entry (uid=1);
    the manager's reconcile task also runs (patched sleep yields
    instantly), but its first fetch sees an empty queue and exits
    cleanly without emitting spurious delta events.
    """
    mgr, _handler = _make_manager(
        _single_page_payload([_ban_entry(uid=1, block_id=101)]),
    )

    received: list[BanListMessage] = []
    cb = _capture_cb(received)

    await mgr.subscribe(cb)
    await mgr.start(room_id=22210347, is_running=_always_running)

    try:
        # The populate snapshot from start() must reach the subscriber.
        snapshots = [m for m in received if m.get("event") == "snapshot"]
        assert snapshots, "no snapshot message broadcast"
        last_snap = snapshots[-1]
        assert last_snap["event"] == "snapshot"
        assert len(last_snap["bans"]) == 1
        assert last_snap["bans"][0]["uid"] == 1
        # Manager state is populated.
        assert 1 in mgr._bans
        assert mgr._bans[1]["id"] == 101
    finally:
        await mgr.stop()


async def test_contract_manager_on_ban_broadcasts_ban_added() -> None:
    """``manager.on_ban(uid, entry)`` MUST insert into ``_bans`` and
    broadcast ``{event: "ban_added", ban: entry}`` to every subscriber.

    The same dict passed in MUST be the ``ban`` field of the broadcast
    message — no transformation / copy.
    """
    mgr, _handler = _make_manager(
        _single_page_payload([_ban_entry(uid=1, block_id=101)]),
    )

    received: list[BanListMessage] = []
    cb = _capture_cb(received)

    await mgr.subscribe(cb)
    await mgr.start(room_id=22210347, is_running=_always_running)

    try:
        new_entry = _ban_entry(uid=2, block_id=102)
        await mgr.on_ban(2, new_entry)

        added = [m for m in received if m.get("event") == "ban_added"]
        assert added, "no ban_added broadcast"
        msg = added[-1]
        assert msg["event"] == "ban_added"
        assert msg["ban"]["uid"] == 2
        assert msg["ban"]["id"] == 102
        # Manager state contains the new uid.
        assert 2 in mgr._bans
        assert mgr._bans[2] == new_entry
    finally:
        await mgr.stop()


async def test_contract_manager_on_unban_broadcasts_ban_removed() -> None:
    """``manager.on_unban(uid)`` MUST drop the entry from ``_bans`` and
    broadcast ``{event: "ban_removed", uid: <int>}`` to subscribers.

    An unban on a uid not present in state is a no-op (no message is
    emitted) — this matches the ``pop(uid, None)`` semantics already
    tested in ``test_banlist.py`` but is repeated here as part of the
    contract suite.
    """
    mgr, _handler = _make_manager(
        _single_page_payload(
            [
                _ban_entry(uid=1, block_id=101),
                _ban_entry(uid=2, block_id=102),
            ]
        ),
    )

    received: list[BanListMessage] = []
    cb = _capture_cb(received)

    await mgr.subscribe(cb)
    await mgr.start(room_id=22210347, is_running=_always_running)

    try:
        await mgr.on_unban(1)

        removed = [m for m in received if m.get("event") == "ban_removed"]
        assert removed, "no ban_removed broadcast"
        msg = removed[-1]
        assert msg["event"] == "ban_removed"
        assert msg["uid"] == 1
        # State reflects the unban.
        assert 1 not in mgr._bans
        # Other entries are untouched.
        assert 2 in mgr._bans
    finally:
        await mgr.stop()


async def test_contract_manager_reconcile_detects_new_uid() -> None:
    """``manager._reconcile`` MUST detect a NEW uid appearing in the
    next synthetic fetch and broadcast ``ban_added`` for it.

    Initial fetch: ``[{uid: 1}]``. Reconcile fetch: ``[{uid: 1},
    {uid: 3}]`` — uid 3 is new. Expected: ``ban_added`` for uid 3.

    The reconcile task fires asynchronously after ``start()``; the test
    polls via ``_wait_for`` until the side-effect materializes. With the
    patched sleep it fires within a handful of yields.
    """
    mgr, _handler = _make_manager(
        _single_page_payload(
            [_ban_entry(uid=1, block_id=101)]
        ),  # start() initial snapshot
        _single_page_payload(
            [  # reconcile cycle: uid 1 + uid 3 (new)
                _ban_entry(uid=1, block_id=101),
                _ban_entry(uid=3, block_id=103),
            ]
        ),
    )

    received: list[BanListMessage] = []
    cb = _capture_cb(received)

    await mgr.subscribe(cb)
    await mgr.start(room_id=22210347, is_running=_always_running)

    try:

        def _predicate() -> bool:
            for m in received:
                if m.get("event") != "ban_added":
                    continue
                ban_dict = cast("dict[str, dict[str, object]]", m)
                uid = ban_dict.get("ban", {}).get("uid")
                if uid == 3:
                    return True
            return False

        ok = await _wait_for(_predicate)
        assert ok, "reconcile never broadcast ban_added for uid 3"

        added_msgs = [
            m
            for m in received
            if m.get("event") == "ban_added"
        ]
        assert added_msgs
        last_ban = cast(
            "dict[str, dict[str, object]]", added_msgs[-1]
        ).get("ban", {})
        assert last_ban.get("uid") == 3
        assert last_ban.get("id") == 103
        assert 3 in mgr._bans
    finally:
        await mgr.stop()


# ===========================================================================
# Error-mapping contract: not silently True / []
# ===========================================================================


async def test_contract_error_permission_denied_surfaces_through_manager_start() -> None:
    """A ban-list fetcher returning code=-403 must surface
    ``PermissionDeniedError`` from ``manager.start`` — NEVER silently
    convert to an empty list / True.

    This is the boundary guarantee: a -403 on the FIRST ban-list fetch
    is a hard "you are not a moderator" failure that downstream code
    must be able to detect. If a future change wraps this as a
    swallowed warning, this test breaks.
    """
    handler = _PageQueueHandler(BAN_PERMISSION_DENIED)
    client = _make_client(handler)
    mgr = BanListManager(bili_client=client, _reconcile_interval=0.01)

    try:
        with pytest.raises(PermissionDeniedError) as excinfo:
            await mgr.start(room_id=22210347, is_running=_always_running)
        assert excinfo.value.code == -403
    finally:
        # start() set _room_id; clean up regardless of whether it raised.
        await mgr.stop()


def test_contract_error_ban_minus_403_never_returns_true() -> None:
    """``ban_user`` with code=-403 MUST raise — MUST NOT silently
    return ``True``. This is the negative companion to scenario 3.

    A regression that converts -403 to a warning + True (e.g. by
    catching ``PermissionDeniedError`` in ``ban_user``) breaks here.
    """
    handler = _static_handler(BAN_PERMISSION_DENIED)
    client = _make_client(handler)
    with pytest.raises(PermissionDeniedError):
        _run(client.ban_user(room_id=22210347, uid=1, hour=1))
