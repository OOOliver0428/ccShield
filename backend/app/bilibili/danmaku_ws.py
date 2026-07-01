"""B站 (Bilibili) live-room WebSocket client — reccshield T11.

Ports the heartbeat / reconnect / queue / watchdog behaviour from
ccShield/app/core/danmaku_ws.py but, by deliberate design:

1. Frame pack/unpack goes through :mod:`app.bilibili.protocol` (T3).
   The ccShield handwritten brace-matcher JSON parser is GONE — we use
   ``protocol.pack_data`` and ``protocol.unpack_data`` exclusively.
2. The class is wired against the typed :class:`BilibiliClient` (T4)
   for room-init, WBI-signed danmu-info, and user-info. No module-level
   ``bili_client`` global.
3. Test seams (constructor kwargs starting with ``_``) let unit tests run
   deterministic, fully-mocked flows without real network or wall-clock
   sleeps. Defaults match the spec: 30 s heartbeat, 45 s watchdog,
   8 s auth timeout, exponential backoff ``[1, 2, 4, 8, 16, 30]`` capped
   at 6 attempts, queue ``maxsize=2000``.

Public API::

    DanmakuClient(room_id, bili_client, on_message=None, **seams)

    async start() -> bool        # init room, auth, start tasks. False on
                                 # fatal auth rejection or no ws within
                                 # _auth_timeout seconds.
    async stop()                 # cancel + close + clear. Bounded.

Behaviour (single-connection only — multi-host redundancy is deferred
to a follow-up; we take ``host_list[0]``):

- Auth rejections (AUTH_RSP ``code != 0``) are FATAL: ``_fatal_error`` is
  set, everything stops, ``start()`` returns ``False`` without retrying.
- Connection drops BEFORE or AFTER auth are recoverable: exponential
  backoff ``[1, 2, 4, 8, 16, 30]`` s, up to 6 attempts.
- Heartbeat is sent every 30 s. If no HEARTBEAT_RSP arrives within 45 s
  the watchdog force-closes the ws, triggering a reconnect.
- All parsed messages flow through a single :class:`asyncio.Queue`
  (``maxsize=2000``) consumed by ``_process_queue``. Drops are logged
  at warning level.

This module owns NO message normalization — that is T12's responsibility.
``on_message`` receives the raw parsed dicts from
``protocol.unpack_data``.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import websockets
from loguru import logger
from websockets.exceptions import ConnectionClosed

from app.bilibili.protocol import (
    AUTH,
    BROTLI,
    HEARTBEAT,
    pack_data,
    unpack_data,
)

if TYPE_CHECKING:
    from app.bilibili.client import BilibiliClient


# ---------------------------------------------------------------------------
# Defaults — overridden by tests via constructor kwargs.
# ---------------------------------------------------------------------------
_DEFAULT_HEARTBEAT_INTERVAL: float = 30.0
_DEFAULT_WATCHDOG_TIMEOUT: float = 45.0
_DEFAULT_AUTH_TIMEOUT: float = 8.0
_DEFAULT_QUEUE_MAXSIZE: int = 2000
_DEFAULT_RECONNECT_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
_DEFAULT_RECONNECT_MAX_ATTEMPTS: int = 6

# Heartbeat body. The B站 protocol allows an opaque payload — this is
# what the web client sends verbatim.
_HEARTBEAT_PAYLOAD: bytes = b'[object Object]'


OnMessageCallback = Callable[[dict], Awaitable[None]]


# Broad ws handle — typed as object because the production
# websockets.ClientConnection and our test FakeWS have incompatible
# send/recv signatures and no common Protocol satisfies both.
WebSocketLike = object


class DanmakuClient:
    """One room's Bili live-WS connection (single host)."""

    def __init__(
        self,
        room_id: int,
        bili_client: BilibiliClient,
        on_message: OnMessageCallback | None = None,
        *,
        _heartbeat_interval: float = _DEFAULT_HEARTBEAT_INTERVAL,
        _watchdog_timeout: float = _DEFAULT_WATCHDOG_TIMEOUT,
        _auth_timeout: float = _DEFAULT_AUTH_TIMEOUT,
        _queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE,
        _reconnect_delays: tuple[float, ...] = _DEFAULT_RECONNECT_DELAYS,
        _reconnect_max_attempts: int = _DEFAULT_RECONNECT_MAX_ATTEMPTS,
    ) -> None:
        self.room_id = room_id
        self.bili_client = bili_client
        self.on_message = on_message

        # Test seams (production defaults match the spec).
        self._heartbeat_interval = _heartbeat_interval
        self._watchdog_timeout = _watchdog_timeout
        self._auth_timeout = _auth_timeout
        self._queue_maxsize = _queue_maxsize
        self._reconnect_delays = tuple(_reconnect_delays)
        self._reconnect_max_attempts = _reconnect_max_attempts

        # Mutable state — initialized in start().
        self.running: bool = False
        self._fatal_error: str | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self.ws: WebSocketLike | None = None
        self.msg_queue: asyncio.Queue[dict] | None = None
        self.uid: int = 0
        self._token: str | None = None

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    async def start(self) -> bool:
        """Initialize room, authenticate, start tasks.

        Returns:
            True on successful auth (≥1 ws connected). False on fatal
            auth rejection, missing danmu-info, or no connection within
            ``_auth_timeout`` seconds.
        """
        logger.info(
            "danmaku_ws: start() called room={} timeout={}s hb={}s",
            self.room_id,
            self._auth_timeout,
            self._heartbeat_interval,
        )
        danmu_info = await self._fetch_danmu_info()
        if danmu_info is None:
            logger.error("danmaku_ws: get_danmu_info failed room={}", self.room_id)
            return False

        token = danmu_info.get("token")
        host_list = danmu_info.get("host_list") or []
        if not token or not host_list:
            logger.error(
                "danmaku_ws: missing token/hosts room={} token={} hosts={}",
                self.room_id,
                bool(token),
                len(host_list),
            )
            return False
        self._token = token

        # Single-connection: take only the first host.
        host = host_list[0]
        ws_url = f"wss://{host['host']}:{host['wss_port']}/sub"
        logger.info(
            "danmaku_ws: danmu_info ok room={} token_len={} hosts={} url={}",
            self.room_id,
            len(token),
            len(host_list),
            ws_url,
        )

        self.uid = await self._fetch_uid()

        # Reset state.
        self.running = True
        self._fatal_error = None
        self._tasks = []
        self.ws = None
        self.msg_queue = asyncio.Queue(maxsize=self._queue_maxsize)

        queue_task = asyncio.create_task(
            self._process_queue(), name=f"danmaku_queue[{self.room_id}]"
        )
        self._tasks.append(queue_task)

        # Run the connect loop in the background; await first auth result.
        connect_task = asyncio.create_task(
            self._connect_loop(ws_url), name=f"danmaku_conn[{self.room_id}]"
        )
        self._tasks.append(connect_task)

        # Wait for first auth outcome (success or fatal) up to _auth_timeout.
        deadline = asyncio.get_running_loop().time() + self._auth_timeout
        while asyncio.get_running_loop().time() < deadline:
            if self._fatal_error is not None:
                logger.error(
                    "danmaku_ws: fatal during start room={}: {}",
                    self.room_id,
                    self._fatal_error,
                )
                await self.stop()
                return False
            if self.ws is not None:
                logger.info(
                    "danmaku_ws: started room={} ws={}", self.room_id, ws_url
                )
                return True
            await asyncio.sleep(0.05)

        logger.error(
            "danmaku_ws: no connection within {:.1f}s room={}",
            self._auth_timeout,
            self.room_id,
        )
        await self.stop()
        return False

    async def stop(self) -> None:
        """Cancel all tasks, close the ws, clear the queue."""
        self.running = False

        tasks_snapshot = list(self._tasks)
        for task in tasks_snapshot:
            if not task.done():
                task.cancel()

        if tasks_snapshot:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks_snapshot, return_exceptions=True),
                    timeout=5.0,
                )
            except TimeoutError:
                logger.warning(
                    "danmaku_ws: stop() task timeout room={}", self.room_id
                )
                for task in tasks_snapshot:
                    if not task.done():
                        task.cancel()

        ws = self.ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.close(), timeout=2.0)  # type: ignore[attr-defined]

        self.ws = None
        self._tasks = []

        if self.msg_queue is not None:
            while not self.msg_queue.empty():
                try:
                    self.msg_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

        logger.info("danmaku_ws: stopped room={}", self.room_id)

    # -----------------------------------------------------------------------
    # Connect / auth / heartbeat / listen / watchdog
    # -----------------------------------------------------------------------

    async def _connect_loop(self, ws_url: str) -> None:
        """Connect → auth → run session; reconnect with backoff on drop.

        Auth failures (AUTH_RSP code != 0) are fatal — they set
        ``self._fatal_error`` and return without retrying. Connection
        drops (including auth timeouts) are recoverable: up to
        ``_reconnect_max_attempts`` attempts with the configured
        backoff schedule.
        """
        attempts = 0
        while self.running and attempts < self._reconnect_max_attempts:
            # 1. Connect.
            try:
                ws = await websockets.connect(
                    ws_url,
                    ping_interval=None,
                    max_size=None,
                    compression=None,
                )
            except Exception as exc:
                logger.warning(
                    "danmaku_ws: connect failed room={}: {}", self.room_id, exc
                )
                attempts += 1
                await self._backoff_sleep(attempts)
                continue

            # 2. Auth.
            try:
                auth_ok, fatal = await asyncio.wait_for(
                    self._send_auth(ws), timeout=self._auth_timeout
                )
            except (TimeoutError, ConnectionClosed):
                auth_ok, fatal = False, False
            except Exception as exc:
                logger.warning(
                    "danmaku_ws: auth raised room={}: {}", self.room_id, exc
                )
                auth_ok, fatal = False, False

            if not auth_ok and fatal:
                # B站 rejected the token — no retry, this is fatal.
                self._fatal_error = "auth rejected by B站"
                with contextlib.suppress(Exception):
                    await ws.close()
                return

            if not auth_ok:
                # Connection lost before / during auth — recoverable.
                logger.warning(
                    "danmaku_ws: auth exchange failed room={}", self.room_id
                )
                with contextlib.suppress(Exception):
                    await ws.close()
                attempts += 1
                await self._backoff_sleep(attempts)
                continue

            # 3. Auth OK — run heartbeat + listen + watchdog.
            self.ws = ws
            attempts = 0  # reset on successful session.
            logger.info("danmaku_ws: auth ok room={}", self.room_id)

            await self._run_session(ws)

            # Session ended — clean up the ws.
            self.ws = None
            with contextlib.suppress(Exception):
                await ws.close()

            if not self.running:
                return

            attempts += 1
            await self._backoff_sleep(attempts)

        if self.running:
            logger.error(
                "danmaku_ws: max reconnect attempts ({}) exceeded room={}",
                self._reconnect_max_attempts,
                self.room_id,
            )

    async def _backoff_sleep(self, attempt: int) -> None:
        """Sleep for the backoff delay corresponding to ``attempt`` (1-indexed)."""
        if not self.running:
            return
        delays = self._reconnect_delays
        if not delays:
            return
        idx = min(attempt - 1, len(delays) - 1)
        await asyncio.sleep(delays[idx])

    async def _send_auth(self, ws: WebSocketLike) -> tuple[bool, bool]:
        """Send AUTH frame; wait for AUTH_RSP.

        Returns:
            (auth_ok, fatal):
              - (True, False):  code == 0 → auth OK
              - (False, True):  code != 0 → fatal (do NOT retry)
              - (False, False): no AUTH_RSP received → recoverable
        """
        assert self._token is not None
        body = json.dumps(
            {
                "uid": self.uid,
                "roomid": self.room_id,
                "protover": 3,
                "platform": "web",
                "type": 2,
                "key": self._token,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        # Match ccShield's proven impl: pack AUTH with protover=3 (BROTLI).
        # B站 expects the AUTH frame's header protover to match the session's
        # declared compression; sending protover=1 here was a known cause
        # of "no danmaku after connect" — auth silently accepted by B站 but
        # subsequent NORMAL frames refused.
        packet = pack_data(body, AUTH, proto_ver=BROTLI)

        logger.info(
            "danmaku_ws: sending AUTH room={} uid={} protover=3 platform=web "
            "key_len={}",
            self.room_id,
            self.uid,
            len(self._token),
        )
        await ws.send(packet)  # type: ignore[attr-defined]
        try:
            resp = await ws.recv()  # type: ignore[attr-defined]
        except ConnectionClosed as exc:
            logger.warning(
                "danmaku_ws: AUTH ws closed during recv room={} code={}",
                self.room_id,
                exc.code,
            )
            return False, False

        for msg in unpack_data(resp):
            code = msg.get("code")
            if code == 0:
                logger.info(
                    "danmaku_ws: AUTH_RSP code=0 room={} (auth ok)",
                    self.room_id,
                )
                return True, False
            if isinstance(code, int):
                logger.error(
                    "danmaku_ws: AUTH_RSP code={} room={} msg={} (fatal)",
                    code,
                    self.room_id,
                    msg.get("msg") or msg.get("message"),
                )
                return False, True
        # No AUTH_RSP-decoded message (e.g. an unrelated frame arrived).
        logger.warning(
            "danmaku_ws: no AUTH_RSP in first recv room={} (recoverable)",
            self.room_id,
        )
        return False, False

    async def _run_session(self, ws: WebSocketLike) -> None:
        """Spawn heartbeat + listen + watchdog; await first to finish."""
        last_ack_box: list[float] = [asyncio.get_running_loop().time()]

        heartbeat_task = asyncio.create_task(
            self._heartbeat(ws), name=f"hb[{self.room_id}]"
        )
        listen_task = asyncio.create_task(
            self._listen(ws, last_ack_box), name=f"listen[{self.room_id}]"
        )
        watchdog_task = asyncio.create_task(
            self._watchdog(ws, last_ack_box), name=f"watchdog[{self.room_id}]"
        )
        self._tasks.extend([heartbeat_task, listen_task, watchdog_task])

        try:
            _done, pending = await asyncio.wait(
                {heartbeat_task, listen_task, watchdog_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            for t in (heartbeat_task, listen_task, watchdog_task):
                if t in self._tasks:
                    self._tasks.remove(t)

    async def _heartbeat(self, ws: WebSocketLike) -> None:
        """Send HEARTBEAT every ``_heartbeat_interval`` seconds."""
        packet = pack_data(_HEARTBEAT_PAYLOAD, HEARTBEAT)
        while self.running:
            try:
                await ws.send(packet)  # type: ignore[attr-defined]
            except (ConnectionClosed, asyncio.CancelledError):
                return
            except Exception as exc:
                logger.debug(
                    "danmaku_ws: heartbeat send failed room={}: {}",
                    self.room_id,
                    exc,
                )
                return
            await asyncio.sleep(self._heartbeat_interval)

    async def _listen(self, ws: WebSocketLike, last_ack_box: list[float]) -> None:
        """Receive frames, parse, forward to msg_queue.

        HEARTBEAT_RSP frames update ``last_ack_box`` (the watchdog reset
        signal). All other messages are enqueued (drop + warn on
        ``QueueFull``). ConnectionClosed terminates the loop.
        """
        frame_count = 0
        while self.running:
            try:
                data = await ws.recv()  # type: ignore[attr-defined]
            except (ConnectionClosed, asyncio.CancelledError):
                return
            except Exception as exc:
                logger.debug(
                    "danmaku_ws: recv failed room={}: {}", self.room_id, exc
                )
                return

            if not isinstance(data, (bytes, bytearray)):
                continue

            frame_count += 1
            if frame_count == 1:
                logger.info(
                    "danmaku_ws: first frame received room={} bytes={}",
                    self.room_id,
                    len(data),
                )
            elif frame_count % 500 == 0:
                logger.info(
                    "danmaku_ws: frames_received={} room={}",
                    frame_count,
                    self.room_id,
                )

            try:
                messages = unpack_data(bytes(data))
            except Exception as exc:
                logger.debug(
                    "danmaku_ws: unpack failed room={}: {}", self.room_id, exc
                )
                continue

            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                if "online_count" in msg:  # HEARTBEAT_RSP
                    last_ack_box[0] = asyncio.get_running_loop().time()
                    continue
                self._enqueue(msg)

    async def _watchdog(self, ws: WebSocketLike, last_ack_box: list[float]) -> None:
        """Force-close ws if no HEARTBEAT_RSP arrives within ``_watchdog_timeout``."""
        while self.running:
            await asyncio.sleep(0.5)
            elapsed = asyncio.get_running_loop().time() - last_ack_box[0]
            if elapsed > self._watchdog_timeout:
                logger.warning(
                    "danmaku_ws: watchdog timeout room={} elapsed={:.1f}s",
                    self.room_id,
                    elapsed,
                )
                with contextlib.suppress(Exception):
                    await ws.close()  # type: ignore[attr-defined]
                return

    async def _process_queue(self) -> None:
        """Single consumer that dispatches queue messages to ``on_message``."""
        assert self.msg_queue is not None
        processed = 0
        cmd_counts: dict[str, int] = {}
        while self.running:
            try:
                msg = await self.msg_queue.get()
            except asyncio.CancelledError:
                return
            cmd = msg.get("cmd") if isinstance(msg, dict) else None
            cmd_key = cmd if isinstance(cmd, str) else "<non-str>"
            cmd_counts[cmd_key] = cmd_counts.get(cmd_key, 0) + 1
            processed += 1
            if processed == 1:
                logger.info(
                    "danmaku_ws: first message dispatched cmd={} room={}",
                    cmd_key,
                    self.room_id,
                )
            elif processed % 200 == 0:
                top = sorted(cmd_counts.items(), key=lambda kv: -kv[1])[:5]
                logger.info(
                    "danmaku_ws: processed={} room={} top_cmds={}",
                    processed,
                    self.room_id,
                    top,
                )
            try:
                if self.on_message is not None:
                    await self.on_message(msg)
            except Exception as exc:
                logger.error(
                    "danmaku_ws: on_message raised room={}: {}",
                    self.room_id,
                    exc,
                )
            finally:
                self.msg_queue.task_done()
        if cmd_counts:
            logger.info(
                "danmaku_ws: queue consumer exit room={} processed={} cmds={}",
                self.room_id,
                processed,
                cmd_counts,
            )

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _enqueue(self, msg: dict) -> None:
        """Non-blocking enqueue; drop + warn on QueueFull."""
        if self.msg_queue is None:
            return
        try:
            self.msg_queue.put_nowait(msg)
        except asyncio.QueueFull:
            logger.warning(
                "danmaku_ws: queue full, dropping msg cmd={} room={}",
                msg.get("cmd"),
                self.room_id,
            )

    async def _fetch_danmu_info(self) -> dict | None:
        try:
            return await self.bili_client.get_danmu_info(self.room_id)
        except Exception as exc:
            logger.error("danmaku_ws: get_danmu_info raised: {}", exc)
            return None

    async def _fetch_uid(self) -> int:
        try:
            user_info = await self.bili_client.get_user_info()
        except Exception as exc:
            logger.warning(
                "danmaku_ws: get_user_info raised, anonymous: {}", exc
            )
            return 0
        if isinstance(user_info, dict):
            mid = user_info.get("mid")
            if isinstance(mid, int):
                logger.info(
                    "danmaku_ws: authenticated uid={} room={}", mid, self.room_id
                )
                return mid
        logger.warning(
            "danmaku_ws: no user_info, anonymous uid=0 room={}", self.room_id
        )
        return 0


__all__ = ["DanmakuClient"]
