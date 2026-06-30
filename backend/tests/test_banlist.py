"""TDD tests for BanListManager (T17).

These tests predate ``app/room/banlist.py`` and are intended to be RUN
FIRST — the import error at collection time is the failing-first proof.
Once the module is implemented each test exercises a single invariant.
No real network, no real timers, no real B站.

Mock strategy
-------------

1. ``_SequencedBiliClient`` — an in-memory fake of
   :class:`app.bilibili.client.BilibiliClient` whose ``get_ban_list``
   pops the next value from a pre-loaded queue. This makes both the
   ``start()`` initial-snapshot fetch and the subsequent reconcile
   fetches deterministic without elaborate ``asyncio.Event`` machinery.

2. ``asyncio.sleep`` is patched per-test (autouse fixture) with a
   helper that yields once to the real loop (``await real_sleep(0)``).
   A bare ``AsyncMock()`` would resolve immediately but NOT actually
   yield control, which would starve the reconcile task. Real
   ``sleep(0)`` gives the scheduler a chance to run it.

3. The reconcile interval test seam (``_reconcile_interval=0.01``)
   keeps the loop firing as fast as the patched sleep allows.

4. A short poll-with-yield loop awaits the reconcile side-effect so
   the tests are deterministic without ``asyncio.Event`` plumbing.

Test count: 9 (one per spec scenario):

  1. ``start`` populates ``_bans`` + broadcasts snapshot
  2. ``subscribe`` after ``start`` immediately receives full snapshot
  3. ``on_ban`` adds + broadcasts ``ban_added``
  4. ``on_unban`` removes + broadcasts ``ban_removed``
  5. ``_reconcile`` detects new ban (out-of-band via B站 web UI)
  6. ``_reconcile`` detects removed ban
  7. ``stop`` cancels the reconcile task and clears state
  8. ``_broadcast`` isolates one raising subscriber from others
  9. ``_fetch_snapshot`` passes the ``is_running`` callback through
"""
from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

# The module under test does NOT exist yet — this import is the
# failing-first proof. Once ``app/room/banlist.py`` lands the rest of
# this file exercises the 9 invariants.

# Real sleep, captured BEFORE any test patches asyncio.sleep. Used by
# the _yield_sleep helper to actually yield control without sleeping.
_real_sleep = asyncio.sleep


async def _yield_sleep(_delay: float) -> None:
    """Patched ``asyncio.sleep`` in tests: yield once, ignore the delay.

    A bare ``AsyncMock()`` would resolve immediately but NOT actually
    yield control, starving the reconcile task. A real ``sleep(0)``
    hands control back to the scheduler so the background task can run.
    """
    await _real_sleep(0)


# ---------------------------------------------------------------------------
# Mock client — deterministic get_ban_list
# ---------------------------------------------------------------------------


class _SequencedBiliClient:
    """Drop-in for ``app.bilibili.client.BilibiliClient`` with queued fetches.

    Each call to :meth:`get_ban_list` pops the next pre-loaded payload
    off ``queue``. If the queue is empty the method returns ``[]``.
    Calls are recorded in ``calls`` (list of dicts) so tests can assert
    on the ``is_running`` callback pass-through.
    """

    def __init__(self, *payloads: list[dict[str, Any]]) -> None:
        self.queue: list[list[dict[str, Any]]] = [list(p) for p in payloads]
        self.calls: list[dict[str, Any]] = []

    async def get_ban_list(
        self,
        room_id: int,
        *,
        is_running: Any = None,
    ) -> list[dict[str, Any]]:
        self.calls.append({"room_id": room_id, "is_running": is_running})
        # Yield once so cancellation in stop() can take effect mid-fetch.
        await _real_sleep(0)
        if not self.queue:
            return []
        return self.queue.pop(0)


def _make_client(*payloads: list[dict[str, Any]]) -> _SequencedBiliClient:
    """Build a sequenced mock client with the given fetch payloads."""
    return _SequencedBiliClient(*payloads)


# ---------------------------------------------------------------------------
# Autouse fixture — patch asyncio.sleep in the banlist module
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_banlist_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``asyncio.sleep`` inside ``app.room.banlist`` (not the
    module that owns this fixture) so the reconcile loop is fast AND
    yields control. The patch is applied per-test so other tests
    are unaffected.
    """
    monkeypatch.setattr("app.room.banlist.asyncio.sleep", _yield_sleep)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _wait_for(predicate: Any, *, max_yields: int = 50) -> bool:
    """Yield to the loop until ``predicate()`` is truthy or we time out.

    Returns the final truthiness of the predicate. Tests that poll for
    reconcile-driven messages use this instead of bare sleeps to keep
    the suite deterministic.
    """
    for _ in range(max_yields):
        if predicate():
            return True
        await asyncio.sleep(0)
    return predicate()


# ---------------------------------------------------------------------------
# 1. start: populate _bans + broadcast snapshot to subscriber
# ---------------------------------------------------------------------------


async def test_start_populates_bans_and_broadcasts_snapshot() -> None:
    """start() fetches the initial snapshot, populates ``_bans``, and
    broadcasts ``{event: 'snapshot', bans: [...]}`` to every registered
    subscriber.
    """
    client = _make_client(
        [{"uid": 1, "uname": "alice"}, {"uid": 2, "uname": "bob"}],
    )
    mgr = _make_manager(client)

    received: list[dict[str, Any]] = []

    async def cb(msg: dict[str, Any]) -> None:
        received.append(msg)

    await mgr.subscribe(cb)
    await mgr.start(room_id=100, is_running=lambda: True)

    try:
        # The populated snapshot message must appear in the stream.
        snapshots = [m for m in received if m.get("event") == "snapshot"]
        populated = [m for m in snapshots if len(m["bans"]) == 2]
        assert len(populated) == 1
        assert sorted(b["uid"] for b in populated[0]["bans"]) == [1, 2]
        # Local state is populated.
        assert set(mgr._bans.keys()) == {1, 2}
        assert mgr._bans[1]["uname"] == "alice"
        assert mgr._bans[2]["uname"] == "bob"
    finally:
        await mgr.stop()


def _make_manager(
    client: _SequencedBiliClient, *, interval: float = 0.01
) -> Any:
    """Build a BanListManager with the small reconcile interval seam."""
    from app.room.banlist import BanListManager

    # ``_SequencedBiliClient`` is a test double — it does not subclass
    # the real client (kept duck-typed to avoid binding tests to the
    # full surface). Cast so basedpyright sees the right type.
    return BanListManager(
        cast("Any", client), _reconcile_interval=interval
    )


# ---------------------------------------------------------------------------
# 2. subscribe late: after start, new subscriber immediately receives snapshot
# ---------------------------------------------------------------------------


async def test_subscribe_after_start_receives_current_snapshot() -> None:
    """A subscriber registered AFTER ``start`` must immediately receive
    the current snapshot so a late WS connect can bootstrap its UI.
    """
    client = _make_client([{"uid": 1}, {"uid": 2}])
    mgr = _make_manager(client)

    await mgr.start(room_id=100, is_running=lambda: True)

    received: list[dict[str, Any]] = []

    async def cb(msg: dict[str, Any]) -> None:
        received.append(msg)

    await mgr.subscribe(cb)

    try:
        snapshots = [m for m in received if m.get("event") == "snapshot"]
        assert len(snapshots) == 1
        assert sorted(b["uid"] for b in snapshots[0]["bans"]) == [1, 2]
    finally:
        await mgr.stop()


# ---------------------------------------------------------------------------
# 3. on_ban: add uid + broadcast ban_added
# ---------------------------------------------------------------------------


async def test_on_ban_adds_uid_and_broadcasts_delta() -> None:
    """on_ban(uid, entry) inserts into ``_bans`` and broadcasts a
    ``ban_added`` delta to every subscriber.
    """
    client = _make_client([{"uid": 1}])
    mgr = _make_manager(client)

    received: list[dict[str, Any]] = []

    async def cb(msg: dict[str, Any]) -> None:
        received.append(msg)

    await mgr.subscribe(cb)
    await mgr.start(room_id=100, is_running=lambda: True)

    try:
        await mgr.on_ban(3, {"uid": 3, "uname": "carol"})

        added = [m for m in received if m.get("event") == "ban_added"]
        assert len(added) == 1
        assert added[0]["ban"]["uid"] == 3
        assert 3 in mgr._bans
        assert mgr._bans[3]["uname"] == "carol"
    finally:
        await mgr.stop()


# ---------------------------------------------------------------------------
# 4. on_unban: remove uid + broadcast ban_removed
# ---------------------------------------------------------------------------


async def test_on_unban_removes_uid_and_broadcasts_delta() -> None:
    """on_unban(uid) drops the entry from ``_bans`` and broadcasts a
    ``ban_removed`` delta.
    """
    client = _make_client([{"uid": 1}, {"uid": 2}])
    mgr = _make_manager(client)

    received: list[dict[str, Any]] = []

    async def cb(msg: dict[str, Any]) -> None:
        received.append(msg)

    await mgr.subscribe(cb)
    await mgr.start(room_id=100, is_running=lambda: True)

    try:
        await mgr.on_unban(1)

        removed = [m for m in received if m.get("event") == "ban_removed"]
        assert len(removed) == 1
        assert removed[0]["uid"] == 1
        assert 1 not in mgr._bans
        assert 2 in mgr._bans  # other entry untouched
    finally:
        await mgr.stop()


# ---------------------------------------------------------------------------
# 5. _reconcile detects new ban (out-of-band via B站 web UI)
# ---------------------------------------------------------------------------


async def test_reconcile_detects_new_ban() -> None:
    """Reconcile fires every ``_reconcile_interval``: if a new uid
    appears in ``get_ban_list`` that is NOT in local ``_bans``, emit
    ``ban_added``. Catches out-of-band bans via B站 web UI.
    """
    client = _make_client(
        [{"uid": 1}],  # initial snapshot: only uid 1
        [{"uid": 1}, {"uid": 2}],  # reconcile cycle 1: uid 2 added
    )
    mgr = _make_manager(client, interval=0.01)

    received: list[dict[str, Any]] = []

    async def cb(msg: dict[str, Any]) -> None:
        received.append(msg)

    await mgr.subscribe(cb)
    await mgr.start(room_id=100, is_running=lambda: True)

    try:
        ok = await _wait_for(
            lambda: any(m.get("event") == "ban_added" for m in received),
            max_yields=100,
        )
        assert ok, "reconcile never broadcast ban_added"

        added = [m for m in received if m.get("event") == "ban_added"]
        assert len(added) == 1
        assert added[0]["ban"]["uid"] == 2
        assert 2 in mgr._bans
    finally:
        await mgr.stop()


# ---------------------------------------------------------------------------
# 6. _reconcile detects removed ban
# ---------------------------------------------------------------------------


async def test_reconcile_detects_removed_ban() -> None:
    """If a uid in local ``_bans`` is missing from the next
    ``get_ban_list``, emit ``ban_removed``.
    """
    client = _make_client(
        [{"uid": 1}, {"uid": 2}],  # initial: 1 and 2
        [{"uid": 1}],  # reconcile: 2 was unband out-of-band
    )
    mgr = _make_manager(client, interval=0.01)

    received: list[dict[str, Any]] = []

    async def cb(msg: dict[str, Any]) -> None:
        received.append(msg)

    await mgr.subscribe(cb)
    await mgr.start(room_id=100, is_running=lambda: True)

    try:
        ok = await _wait_for(
            lambda: any(m.get("event") == "ban_removed" for m in received),
            max_yields=100,
        )
        assert ok, "reconcile never broadcast ban_removed"

        removed = [m for m in received if m.get("event") == "ban_removed"]
        assert len(removed) == 1
        assert removed[0]["uid"] == 2
        assert 2 not in mgr._bans
        assert 1 in mgr._bans  # 1 still banned
    finally:
        await mgr.stop()


# ---------------------------------------------------------------------------
# 7. stop() cancels reconcile + clears state
# ---------------------------------------------------------------------------


async def test_stop_cancels_reconcile_and_clears_state() -> None:
    """``stop()`` cancels the reconcile task and clears ``_bans`` +
    subscribers. After stop, no further broadcasts originate from
    reconcile (any new reconcile cycle is dead).
    """
    client = _make_client([{"uid": 1}])
    mgr = _make_manager(client, interval=0.01)

    await mgr.subscribe(_noop_cb)
    await mgr.start(room_id=100, is_running=lambda: True)

    assert mgr._reconcile_task is not None
    assert mgr._room_id == 100
    assert mgr._bans == {1: {"uid": 1}}

    await mgr.stop()

    assert mgr._reconcile_task is None
    assert mgr._room_id is None
    assert mgr._bans == {}
    assert mgr._subscribers == []


async def _noop_cb(_msg: dict[str, Any]) -> None:
    """Silent subscriber used when a test only cares about state, not msgs."""
    return None


# ---------------------------------------------------------------------------
# 8. _broadcast isolates one raising subscriber from the others
# ---------------------------------------------------------------------------


async def test_broadcast_isolates_subscriber_errors() -> None:
    """A subscriber that raises must NOT prevent other subscribers from
    being invoked. This mirrors T12's broadcast error-isolation rule.
    """
    client = _make_client([{"uid": 1}])
    mgr = _make_manager(client)

    good1: list[dict[str, Any]] = []
    good2: list[dict[str, Any]] = []

    async def good(msg: dict[str, Any]) -> None:
        good1.append(msg)

    async def bad(msg: dict[str, Any]) -> None:
        raise RuntimeError("subscriber boom")

    async def good_other(msg: dict[str, Any]) -> None:
        good2.append(msg)

    await mgr.subscribe(good)
    await mgr.subscribe(bad)
    await mgr.subscribe(good_other)

    await mgr.start(room_id=100, is_running=lambda: True)

    try:
        # good subscribers must receive both the empty pre-start
        # snapshot (subscribe fired it) AND the populated start
        # snapshot — the bad subscriber's raise must not block them.
        assert len(good1) >= 1
        assert len(good2) >= 1
        # The last message each good subscriber received must be the
        # populated start snapshot.
        assert good1[-1]["event"] == "snapshot"
        assert good2[-1]["event"] == "snapshot"
        assert len(good1[-1]["bans"]) == 1
        assert len(good2[-1]["bans"]) == 1
    finally:
        await mgr.stop()


# ---------------------------------------------------------------------------
# 9. _fetch_snapshot passes the is_running callback through
# ---------------------------------------------------------------------------


async def test_fetch_snapshot_passes_is_running_callback_to_client() -> None:
    """The ``is_running`` callback is forwarded to
    ``bili_client.get_ban_list`` so T4 can short-circuit pagination
    when the room is no longer active.
    """
    client = _make_client([{"uid": 1}])
    mgr = _make_manager(client, interval=0.01)

    is_running = lambda: True  # noqa: E731

    await mgr.start(room_id=100, is_running=is_running)

    try:
        assert len(client.calls) >= 1
        # Every call must have passed through the is_running kwarg.
        for call in client.calls:
            assert call["room_id"] == 100
            assert call["is_running"] is is_running
    finally:
        await mgr.stop()


# ---------------------------------------------------------------------------
# Module-level singleton get/set
# ---------------------------------------------------------------------------


async def test_module_singleton_get_set_round_trip() -> None:
    """``banlist_manager`` is a module-level singleton; ``get_/set_``
    helpers round-trip a manager instance.
    """
    import app.room.banlist as banlist_mod

    original: Any = banlist_mod.banlist_manager
    try:
        banlist_mod.banlist_manager = None

        assert banlist_mod.get_banlist_manager() is None
        assert banlist_mod.banlist_manager is None

        client = _make_client([])
        mgr = _make_manager(client, interval=0.01)
        banlist_mod.set_banlist_manager(mgr)

        assert banlist_mod.get_banlist_manager() is mgr
        assert banlist_mod.banlist_manager is mgr

        banlist_mod.set_banlist_manager(None)
        assert banlist_mod.get_banlist_manager() is None
    finally:
        banlist_mod.banlist_manager = original
