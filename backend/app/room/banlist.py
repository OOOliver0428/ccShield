"""Ban-list manager (T17) — WS-driven state + backend reconcile.

This module owns the live-room ban-list state for reccshield. The
:class:`BanListManager` is the source of truth for the current ban
state; subscribers (T18's WS route) receive push notifications shaped
as one of three wire messages:

* ``{"event": "snapshot",  "bans": [...]}`` — full state on (re)connect.
* ``{"event": "ban_added", "ban":  {...}}`` — single new ban (WS push).
* ``{"event": "ban_removed", "uid": <int>}`` — single unban (WS push).

Why this exists
---------------

ccShield polled the B站 ban-list every few seconds per room
(``LOG_ANALYSIS_FINAL.md``). That generated a request storm against
the API, and any ban the operator performed via the B站 web UI was
already stale by the time the next poll fired. We REPLACE polling with
WS push (T18 wires the bridge) plus a backend **60-second reconcile**
that re-fetches ``get_ban_list`` and emits deltas — this catches
out-of-band bans that never crossed our WS path (e.g. an admin on the
B站 dashboard).

Single active room
------------------

``start(room_id)`` brings the manager up for ONE room. Calling
``start`` again or for a different room is not supported in this
layer — T16 owns room lifecycle via the local-token WS, and the
caller (T18) is expected to ``stop()`` before re-using the manager.
The module-level singleton (``banlist_manager``) is the canonical
handle for the currently active room.

Test seam
---------

``_reconcile_interval`` (default ``60.0``) is the loop period. Tests
inject a tiny value (``0.01``) and the ``asyncio.sleep`` patch in
``tests/test_banlist.py`` yields once per call so the reconcile loop
runs deterministically without wall-clock time.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Literal, TypedDict

from loguru import logger

if TYPE_CHECKING:
    from app.bilibili.client import BilibiliClient


# ---------------------------------------------------------------------------
# Wire messages (TypedDict — typed at static-check time, plain dict at
# runtime so WS JSON serialization stays trivial).
# ---------------------------------------------------------------------------


BanTimestamp = int | str | None


class BanEntry(TypedDict):
    """Stable ban-list shape shared by REST, WS and the frontend."""

    block_id: int | None
    uid: int
    uname: str
    operator_uid: int | None
    operator_name: str
    hour: int | None
    reason: str
    created_at: BanTimestamp
    expires_at: BanTimestamp
    pending: bool


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lstrip("-").isdigit():
            return int(stripped)
    return None


def _as_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_timestamp(value: object) -> BanTimestamp:
    numeric = _as_int(value)
    if numeric is not None:
        return numeric
    text = _as_text(value)
    return text or None


def _duration_hours(
    created_at: BanTimestamp,
    expires_at: BanTimestamp,
) -> int | None:
    """Derive a display duration from B站's start/end timestamps."""
    if isinstance(expires_at, str):
        label = expires_at.strip()
        if label == "永久":
            return -1
        if label in {"本场", "本场直播"}:
            return 0

    if isinstance(created_at, int) and isinstance(expires_at, int):
        seconds = expires_at - created_at
    elif isinstance(created_at, str) and isinstance(expires_at, str):
        try:
            seconds = (
                datetime.fromisoformat(expires_at)
                - datetime.fromisoformat(created_at)
            ).total_seconds()
        except (TypeError, ValueError):
            return None
    else:
        return None

    if seconds <= 0:
        return None
    return max(1, round(seconds / 3600))


def normalize_ban_entry(
    raw: Mapping[str, object],
    *,
    pending: bool = False,
) -> BanEntry | None:
    """Normalize known B站 field aliases into one safe public shape.

    Invalid/missing block ids do not discard an otherwise useful list row;
    the frontend displays the user but disables 解禁 until a later refresh
    supplies the authoritative id.
    """
    # In GetSilentUserList's real payload, uid/name identify the moderator
    # who created the record; tuid/tname identify the muted user. Prefer the
    # target fields whenever they are present, falling back to the normalized
    # uid/uname shape used by our own pending records and synthetic tests.
    target_uid = _as_int(raw.get("tuid"))
    uid = target_uid if target_uid is not None and target_uid > 0 else _as_int(
        raw.get("uid")
    )
    if uid is None or uid <= 0:
        return None

    block_id = _as_int(raw.get("block_id"))
    if block_id is None:
        block_id = _as_int(raw.get("id"))
    if block_id is not None and block_id <= 0:
        block_id = None

    if target_uid is not None and target_uid > 0:
        uname = _as_text(raw.get("tname")) or _as_text(raw.get("uname"))
    else:
        uname = _as_text(raw.get("uname")) or _as_text(raw.get("tname"))

    # Preserve explicit normalized fields when a cached entry passes through
    # this function again. For Bilibili's raw GetSilentUserList payload,
    # uid/name belong to the moderator only when tuid identifies the target.
    # Without that guard our own pending uid/uname record would incorrectly
    # claim that the muted user performed the moderation action.
    operator_uid = _as_int(raw.get("operator_uid"))
    operator_name = _as_text(raw.get("operator_name"))
    if target_uid is not None and target_uid > 0:
        if operator_uid is None:
            operator_uid = _as_int(raw.get("uid"))
        if not operator_name:
            operator_name = _as_text(raw.get("name"))
    if operator_uid is not None and operator_uid <= 0:
        operator_uid = None

    hour = _as_int(raw.get("hour"))
    reason = (
        _as_text(raw.get("reason"))
        or _as_text(raw.get("msg"))
        or _as_text(raw.get("message"))
    )
    created_at = _as_timestamp(
        raw.get("created_at", raw.get("ctime", raw.get("start_time")))
    )
    expires_at = _as_timestamp(raw.get("expires_at"))
    if expires_at is None:
        expires_at = _as_timestamp(raw.get("expire_time"))
    if expires_at is None:
        expires_at = _as_timestamp(raw.get("end_time"))
    if expires_at is None:
        expires_at = _as_timestamp(raw.get("block_end_time"))
    if hour is None:
        hour = _duration_hours(created_at, expires_at)
    if (
        expires_at is None
        and isinstance(created_at, int)
        and hour is not None
        and hour > 0
    ):
        expires_at = created_at + hour * 3600

    raw_pending = raw.get("pending")
    return BanEntry(
        block_id=block_id,
        uid=uid,
        uname=uname,
        operator_uid=operator_uid,
        operator_name=operator_name,
        hour=hour,
        reason=reason,
        created_at=created_at,
        expires_at=expires_at,
        pending=pending or raw_pending is True,
    )


class _SnapshotMessage(TypedDict):
    """Full-state push sent on subscribe / start."""

    event: Literal["snapshot"]
    bans: list[BanEntry]


class _BanAddedMessage(TypedDict):
    """Single new-ban delta (from on_ban or reconcile)."""

    event: Literal["ban_added"]
    ban: BanEntry


class _BanRemovedMessage(TypedDict):
    """Single unban delta (from on_unban or reconcile)."""

    event: Literal["ban_removed"]
    uid: int


# Closed union of every wire message a subscriber may receive.
BanListMessage = _SnapshotMessage | _BanAddedMessage | _BanRemovedMessage


# A subscriber receives one message at a time. The annotation stays
# explicit (no Any on the callback's argument type).
BanListCallback = Callable[[BanListMessage], Awaitable[None]]


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class BanListManager:
    """Owns the ban-list state for ONE active room.

    Lifecycle:

    * :meth:`start` fetches the initial snapshot via T4's
      ``get_ban_list`` (which paginates internally and consults
      ``is_running`` per page), populates ``_bans``, broadcasts the
      snapshot to all current subscribers, and starts the background
      reconcile task.
    * :meth:`stop` cancels the reconcile task, clears ``_bans`` and
      the subscriber list. Idempotent.
    * :meth:`subscribe` registers a callback and immediately sends the
      current snapshot to that callback (a late WS connect needs the
      full state to bootstrap its UI).
    * :meth:`unsubscribe` removes a callback if registered.
    * :meth:`on_ban` / :meth:`on_unban` are called by the T18 bridge
      after a ban / unban succeeds via the local API — they update
      local state and broadcast a single delta.
    * :meth:`_reconcile` runs every ``_reconcile_interval`` seconds,
      re-fetches ``get_ban_list``, diffs against ``_bans``, and emits
      ``ban_added`` / ``ban_removed`` deltas for any out-of-band
      changes (e.g. bans made via the B站 web UI).
    """

    def __init__(
        self,
        bili_client: BilibiliClient,
        *,
        _reconcile_interval: float = 60.0,
    ) -> None:
        self.bili_client = bili_client
        self._reconcile_interval = _reconcile_interval

        # Local state — uid → normalized ban entry.
        self._bans: dict[int, BanEntry] = {}

        # Subscribers under a lock so a snapshot taken in ``_broadcast``
        # is consistent with the underlying list at that moment
        # (subsequent add/remove do not affect the in-flight snapshot).
        self._subscribers: list[BanListCallback] = []
        self._subscribers_lock = asyncio.Lock()

        # Background reconcile task (None when not running).
        self._reconcile_task: asyncio.Task[None] | None = None

        # Active room + state-check callback (T4's get_ban_list polls
        # this per page so it can short-circuit when the room is gone).
        self._room_id: int | None = None
        self._is_running: Callable[[], bool] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(
        self,
        room_id: int,
        is_running: Callable[[], bool],
    ) -> None:
        """Set up state for ``room_id`` and start the reconcile task.

        Fetches the initial snapshot (paginated via T4's
        ``get_ban_list``, which consults ``is_running`` per page),
        populates ``_bans``, and broadcasts the snapshot to every
        currently-registered subscriber.

        Single-active-room: if a previous ``start`` is still alive
        (reconcile task non-None) we stop it first. Callers should
        normally call ``stop`` themselves, but this guard makes
        ``start`` safe to call twice in a row without leaking tasks.
        """
        if self._reconcile_task is not None:
            await self.stop()

        self._room_id = room_id
        self._is_running = is_running

        await self.refresh(preserve_pending=False)

        self._reconcile_task = asyncio.create_task(self._reconcile())

    async def stop(self) -> None:
        """Cancel reconcile + clear state. Idempotent.

        Order matters: we null out ``_reconcile_task`` BEFORE awaiting
        the cancellation, so a concurrent ``start`` cannot see a stale
        task handle and double-cancel.
        """
        if self._reconcile_task is not None:
            task = self._reconcile_task
            self._reconcile_task = None
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        self._room_id = None
        self._is_running = None
        self._bans.clear()
        async with self._subscribers_lock:
            self._subscribers.clear()

    # ------------------------------------------------------------------
    # Subscribers
    # ------------------------------------------------------------------

    async def subscribe(self, cb: BanListCallback) -> None:
        """Register ``cb`` and immediately send the current snapshot.

        The snapshot is always sent — even if it is empty (no room
        active yet). This keeps the ordering deterministic: a WS
        client that connects after ``start`` gets the populated
        snapshot exactly once.
        """
        async with self._subscribers_lock:
            self._subscribers.append(cb)
        snapshot_entries: list[BanEntry] = list(self._bans.values())
        await self._safe_invoke(cb, {"event": "snapshot", "bans": snapshot_entries})

    async def unsubscribe(self, cb: BanListCallback) -> None:
        """Remove ``cb`` if registered. No-op if not present."""
        async with self._subscribers_lock:
            with contextlib.suppress(ValueError):
                self._subscribers.remove(cb)

    # ------------------------------------------------------------------
    # Delta events (called by T18 bridge after a successful ban / unban)
    # ------------------------------------------------------------------

    async def on_ban(
        self, uid: int, ban_entry: Mapping[str, object]
    ) -> None:
        """Insert ``uid`` into local state and broadcast ``ban_added``.

        Called by the T18 bridge right after ``bili_client.ban_user``
        returns success — the local API is the source of truth for
        this delta, so reconcile does not need to re-fetch to confirm.
        """
        normalized = normalize_ban_entry(
            ban_entry,
            pending=ban_entry.get("pending") is True,
        )
        if normalized is None or normalized["uid"] != uid:
            raise ValueError("ban entry uid does not match target uid")
        self._bans[uid] = normalized
        await self._broadcast({"event": "ban_added", "ban": normalized})

    async def on_unban(self, uid: int) -> None:
        """Remove ``uid`` from local state and broadcast ``ban_removed``.

        ``pop(uid, None)`` is a no-op if the uid is not present —
        unban idempotency.
        """
        self._bans.pop(uid, None)
        await self._broadcast({"event": "ban_removed", "uid": uid})

    async def refresh(self, *, preserve_pending: bool = True) -> list[BanEntry]:
        """Fetch authoritative state, replace the cache and push a snapshot.

        Pending rows originate from a successful write whose B站 list entry
        has not appeared yet. Keeping them across an immediate/manual refresh
        avoids a confusing disappear/reappear flicker; a fetched row for the
        same uid always wins and supplies the block id.
        """
        entries = await self._fetch_snapshot()
        next_bans = {entry["uid"]: entry for entry in entries}
        if preserve_pending:
            for uid, entry in self._bans.items():
                if entry["pending"] and uid not in next_bans:
                    next_bans[uid] = entry
        self._bans = next_bans
        snapshot = list(self._bans.values())
        await self._broadcast({"event": "snapshot", "bans": snapshot})
        return snapshot

    # ------------------------------------------------------------------
    # Background reconcile — catches out-of-band bans
    # ------------------------------------------------------------------

    async def _reconcile(self) -> None:
        """Periodically re-fetch the ban-list and broadcast deltas.

        Loop invariant: ``while self._room_id is not None`` so a
        concurrent ``stop`` that nulls ``_room_id`` lets us exit on
        the next iteration check. The sleep itself is also a
        cancellation point — ``stop`` cancels the task directly to
        short-circuit.

        Cancellation: ``CancelledError`` propagates out of the sleep
        naturally. A ``CancelledError`` raised mid-fetch
        (inside ``_fetch_snapshot``) is re-raised so ``stop`` sees it.
        Other exceptions are logged and the loop continues — a single
        transient B站 API hiccup must not kill the reconcile.
        """
        while self._room_id is not None:
            await asyncio.sleep(self._reconcile_interval)
            if self._room_id is None:
                break

            try:
                new_entries = await self._fetch_snapshot()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "banlist reconcile: fetch failed room={} err={!r}",
                    self._room_id,
                    exc,
                )
                continue

            new_uids: set[int] = {entry["uid"] for entry in new_entries}
            existing_uids: set[int] = set(self._bans.keys())
            pending_uids = {
                uid for uid, entry in self._bans.items() if entry["pending"]
            }

            added_uids = new_uids - existing_uids
            removed_uids = existing_uids - new_uids - pending_uids

            # Apply adds — refresh entry silently for unchanged uids so
            # local state stays in sync with the server even when the
            # entry data (e.g. ban duration) drifts.
            for entry in new_entries:
                uid = entry["uid"]
                if uid in added_uids:
                    self._bans[uid] = entry
                    await self._broadcast(
                        {"event": "ban_added", "ban": entry}
                    )
                else:
                    self._bans[uid] = entry

            # Apply removes.
            for uid in removed_uids:
                self._bans.pop(uid, None)
                await self._broadcast({"event": "ban_removed", "uid": uid})

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _broadcast(self, msg: BanListMessage) -> None:
        """Snapshot subscribers (under lock), await each (errors logged).

        A raising subscriber MUST NOT prevent the others from being
        invoked (mirrors T12's broadcast error-isolation rule).
        """
        async with self._subscribers_lock:
            snapshot = list(self._subscribers)
        for cb in snapshot:
            await self._safe_invoke(cb, msg)

    async def _safe_invoke(
        self, cb: BanListCallback, msg: BanListMessage
    ) -> None:
        """Await ``cb(msg)``; log and swallow any exception.

        Used by both ``_broadcast`` (fan-out) and ``subscribe`` (single
        late-subscriber snapshot) so the error contract is the same
        regardless of entry point.
        """
        try:
            await cb(msg)
        except Exception as exc:
            logger.error(
                "banlist: subscriber raised room={} err={!r}",
                self._room_id,
                exc,
            )

    async def _fetch_snapshot(self) -> list[BanEntry]:
        """Call ``bili_client.get_ban_list`` (T4 owns pagination).

        ``is_running`` is forwarded so T4 can short-circuit pagination
        if the room is no longer active. Returns ``[]`` if the manager
        has no active room (defensive — should not happen in practice).
        """
        if self._room_id is None or self._is_running is None:
            return []
        raw_entries = await self.bili_client.get_ban_list(
            self._room_id, is_running=self._is_running
        )
        normalized: list[BanEntry] = []
        for raw in raw_entries:
            if not isinstance(raw, dict):
                continue
            entry = normalize_ban_entry(raw)
            if entry is not None:
                normalized.append(entry)
        return normalized


# ---------------------------------------------------------------------------
# Module-level singleton — single active room
# ---------------------------------------------------------------------------


banlist_manager: BanListManager | None = None


def get_banlist_manager() -> BanListManager | None:
    """Return the current manager (or ``None`` if not started)."""
    return banlist_manager


def set_banlist_manager(mgr: BanListManager | None) -> None:
    """Set (or clear) the module-level singleton.

    Uses ``global`` to rebind — the singleton is a single module-level
    name, so callers can either import ``banlist_manager`` directly or
    use the helpers.
    """
    global banlist_manager
    banlist_manager = mgr


__all__ = [
    "BanEntry",
    "BanListCallback",
    "BanListManager",
    "BanListMessage",
    "BanTimestamp",
    "banlist_manager",
    "get_banlist_manager",
    "normalize_ban_entry",
    "set_banlist_manager",
]
