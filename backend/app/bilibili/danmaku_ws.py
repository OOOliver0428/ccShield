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
   at 6 attempts, priority buffer ``maxsize=20000``.

Public API::

    DanmakuClient(room_id, bili_client, on_message=None, **seams)

    async start() -> bool        # init room, auth, start tasks. False on
                                 # fatal auth rejection or no ws within
                                 # _auth_timeout seconds.
    async stop()                 # cancel + close + clear. Bounded.

Behaviour (single-connection only — endpoints rotate on reconnect, but
multiple upstream sockets are never opened in parallel):

- Auth rejections (AUTH_RSP ``code != 0``) are FATAL: ``_fatal_error`` is
  set, everything stops, ``start()`` returns ``False`` without retrying.
- Connection drops BEFORE or AFTER auth are recoverable: exponential
  backoff ``[1, 2, 4, 8, 16, 30]`` s, up to 6 attempts. Each attempt
  uses the next endpoint returned by Bilibili.
- Heartbeat is sent every 30 s. If no HEARTBEAT_RSP arrives within 45 s
  the watchdog force-closes the ws, triggering a reconnect.
- Parsed messages flow through a bounded priority buffer (``maxsize=20000``)
  consumed by ``_process_queue``. Chat, SC and room-management events can
  evict lower-value traffic under pressure. Drops are counted and rate-limited
  in logs so an overload cannot create a second log storm.

This module owns NO message normalization — that is T12's responsibility.
``on_message`` receives the raw parsed dicts from
``protocol.unpack_data``.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import websockets
from loguru import logger
from websockets.exceptions import ConnectionClosed

from app.bilibili.exceptions import AuthExpiredError
from app.bilibili.message_buffer import (
    BufferedMessage,
    MessagePriority,
    PriorityMessageBuffer,
    command_name,
)
from app.bilibili.protocol import (
    AUTH,
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
_DEFAULT_QUEUE_MAXSIZE: int = 20_000
_WS_FRAME_MAX_QUEUE: int = 256
_DEFAULT_RECONNECT_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
_DEFAULT_RECONNECT_MAX_ATTEMPTS: int = 6

# Heartbeat body. The B站 protocol allows an opaque payload — this is
# what the web client sends verbatim.
_HEARTBEAT_PAYLOAD: bytes = b'[object Object]'


OnMessageCallback = Callable[[dict], Awaitable[None]]


@dataclass(slots=True)
class _DanmakuMetrics:
    frames_received: int = 0
    decoded_messages: int = 0
    parse_errors: int = 0
    enqueued_messages: int = 0
    processed_messages: int = 0
    dropped_messages: int = 0
    queue_peak: int = 0
    max_dispatch_lag_ms: float = 0.0
    connection_attempts: int = 0
    connected_sessions: int = 0
    dropped_by_priority: dict[str, int] = field(default_factory=dict)
    dropped_by_command: dict[str, int] = field(default_factory=dict)


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
        self.msg_queue: PriorityMessageBuffer | None = None
        self._metrics = _DanmakuMetrics()
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
        if (
            not isinstance(token, str)
            or not token
            or not isinstance(host_list, list)
        ):
            logger.error(
                "danmaku_ws: missing token/hosts room={} token={} hosts={}",
                self.room_id,
                bool(token),
                len(host_list) if isinstance(host_list, list) else 0,
            )
            return False

        ws_urls: list[str] = []
        for host in host_list:
            if not isinstance(host, dict):
                continue
            hostname = host.get("host")
            wss_port = host.get("wss_port")
            if (
                isinstance(hostname, str)
                and hostname
                and isinstance(wss_port, int)
                and wss_port > 0
            ):
                ws_urls.append(f"wss://{hostname}:{wss_port}/sub")
        if not ws_urls:
            logger.error(
                "danmaku_ws: no valid wss endpoints room={} hosts={}",
                self.room_id,
                len(host_list),
            )
            return False
        self._token = token

        logger.info(
            "danmaku_ws: danmu_info ok room={} token_len={} endpoints={}",
            self.room_id,
            len(token),
            len(ws_urls),
        )

        self.uid = await self._fetch_uid()

        # Reset state.
        self.running = True
        self._fatal_error = None
        self._tasks = []
        self.ws = None
        self._metrics = _DanmakuMetrics()
        self.msg_queue = PriorityMessageBuffer(maxsize=self._queue_maxsize)

        queue_task = asyncio.create_task(
            self._process_queue(), name=f"danmaku_queue[{self.room_id}]"
        )
        self._tasks.append(queue_task)

        # Run the connect loop in the background; await first auth result.
        connect_task = asyncio.create_task(
            self._connect_loop(tuple(ws_urls)),
            name=f"danmaku_conn[{self.room_id}]",
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
                    "danmaku_ws: started room={} endpoints={}",
                    self.room_id,
                    len(ws_urls),
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
            self.msg_queue.clear()

        logger.info("danmaku_ws: stopped room={}", self.room_id)

    # -----------------------------------------------------------------------
    # Connect / auth / heartbeat / listen / watchdog
    # -----------------------------------------------------------------------

    async def _connect_loop(self, ws_urls: tuple[str, ...]) -> None:
        """Connect → auth → run session; reconnect with backoff on drop.

        Auth failures (AUTH_RSP code != 0) are fatal — they set
        ``self._fatal_error`` and return without retrying. Connection
        drops (including auth timeouts) are recoverable: up to
        ``_reconnect_max_attempts`` attempts with the configured
        backoff schedule.
        """
        if not ws_urls:
            self._fatal_error = "no websocket endpoint"
            return

        attempts = 0
        endpoint_cursor = 0
        while self.running and attempts < self._reconnect_max_attempts:
            # 1. Connect.
            ws_url = ws_urls[endpoint_cursor % len(ws_urls)]
            endpoint_cursor += 1
            self._metrics.connection_attempts += 1
            try:
                ws = await websockets.connect(
                    ws_url,
                    ping_interval=None,
                    max_size=None,
                    max_queue=_WS_FRAME_MAX_QUEUE,
                    compression=None,
                )
            except Exception as exc:
                logger.warning(
                    "danmaku_ws: connect failed room={} url={}: {}",
                    self.room_id,
                    ws_url,
                    exc,
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
            self._metrics.connected_sessions += 1
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
        auth_payload: dict[str, object] = {
            "uid": self.uid,
            "roomid": self.room_id,
            # Select Brotli for NORMAL packets sent by the server. This does
            # not describe the encoding of the AUTH packet itself.
            "protover": 3,
            "platform": "web",
            "type": 2,
            "key": self._token,
        }
        buvid = self.bili_client.get_cookie("buvid3")
        if buvid:
            # The cookie is named buvid3; the WS auth field is named buvid.
            auth_payload["buvid"] = buvid

        body = json.dumps(
            auth_payload,
            separators=(",", ":"),
        ).encode("utf-8")
        # AUTH and HEARTBEAT bodies are raw (header protocol version 1).
        # The JSON protover above independently negotiates Brotli downlink.
        packet = pack_data(body, AUTH)

        logger.info(
            "danmaku_ws: sending AUTH room={} uid={} header_ver=1 "
            "message_protover=3 platform=web key_len={} buvid={}",
            self.room_id,
            self.uid,
            len(self._token),
            bool(buvid),
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
        signal). All other messages enter the bounded priority buffer;
        overload shedding is counted by ``_enqueue``. ConnectionClosed
        terminates the loop.
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
            self._metrics.frames_received += 1
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
                self._metrics.parse_errors += 1
                logger.debug(
                    "danmaku_ws: unpack failed room={}: {}", self.room_id, exc
                )
                continue

            self._metrics.decoded_messages += len(messages)
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
                buffered: BufferedMessage = await self.msg_queue.get()
            except asyncio.CancelledError:
                return
            msg = buffered.payload
            lag_ms = (
                asyncio.get_running_loop().time() - buffered.enqueued_at
            ) * 1000
            self._metrics.max_dispatch_lag_ms = max(
                self._metrics.max_dispatch_lag_ms,
                lag_ms,
            )
            cmd = msg.get("cmd") if isinstance(msg, dict) else None
            cmd_key = command_name(msg) if isinstance(cmd, str) else "<non-str>"
            cmd_counts[cmd_key] = cmd_counts.get(cmd_key, 0) + 1
            processed += 1
            self._metrics.processed_messages += 1
            if processed == 1:
                logger.info(
                    "danmaku_ws: first message dispatched cmd={} room={}",
                    cmd_key,
                    self.room_id,
                )
            elif processed % 1000 == 0:
                top = sorted(cmd_counts.items(), key=lambda kv: -kv[1])[:5]
                logger.info(
                    "danmaku_ws: processed={} room={} queue={} peak={} "
                    "dropped={} lag_max_ms={:.1f} top_cmds={}",
                    processed,
                    self.room_id,
                    self.msg_queue.qsize(),
                    self._metrics.queue_peak,
                    self._metrics.dropped_messages,
                    self._metrics.max_dispatch_lag_ms,
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
        """Non-blocking priority enqueue with observable load shedding."""
        if self.msg_queue is None:
            return
        result = self.msg_queue.put_nowait(
            msg,
            enqueued_at=asyncio.get_running_loop().time(),
        )
        if result.accepted:
            self._metrics.enqueued_messages += 1
            self._metrics.queue_peak = max(
                self._metrics.queue_peak,
                self.msg_queue.qsize(),
            )
        if result.dropped is not None:
            self._record_drop(result.dropped, evicted=result.accepted)

    def _record_drop(
        self, dropped: BufferedMessage, *, evicted: bool
    ) -> None:
        self._metrics.dropped_messages += 1
        priority = dropped.priority.name.lower()
        command = command_name(dropped.payload)
        priority_count = self._metrics.dropped_by_priority.get(priority, 0) + 1
        command_count = self._metrics.dropped_by_command.get(command, 0) + 1
        self._metrics.dropped_by_priority[priority] = priority_count
        self._metrics.dropped_by_command[command] = command_count

        # First occurrence and every 100th thereafter. Avoid turning an
        # already-overloaded event loop into a warning-log generator.
        if command_count != 1 and command_count % 100 != 0:
            return
        log = (
            logger.error
            if dropped.priority is MessagePriority.CRITICAL
            else logger.warning
        )
        log(
            "danmaku_ws: buffer pressure {} cmd={} priority={} room={} "
            "dropped_total={} queue={}",
            "evicted" if evicted else "rejected",
            command,
            priority,
            self.room_id,
            self._metrics.dropped_messages,
            self.msg_queue.qsize() if self.msg_queue is not None else 0,
        )

    @property
    def metrics_snapshot(self) -> dict[str, object]:
        """Cheap diagnostic snapshot for logs, tests and future status UI."""
        return {
            "frames_received": self._metrics.frames_received,
            "decoded_messages": self._metrics.decoded_messages,
            "parse_errors": self._metrics.parse_errors,
            "enqueued_messages": self._metrics.enqueued_messages,
            "processed_messages": self._metrics.processed_messages,
            "dropped_messages": self._metrics.dropped_messages,
            "dropped_by_priority": dict(self._metrics.dropped_by_priority),
            "dropped_by_command": dict(self._metrics.dropped_by_command),
            "queue_depth": self.msg_queue.qsize() if self.msg_queue else 0,
            "queue_peak": self._metrics.queue_peak,
            "max_dispatch_lag_ms": round(
                self._metrics.max_dispatch_lag_ms, 3
            ),
            "connection_attempts": self._metrics.connection_attempts,
            "connected_sessions": self._metrics.connected_sessions,
        }

    async def _fetch_danmu_info(self) -> dict | None:
        try:
            return await self.bili_client.get_danmu_info(self.room_id)
        except AuthExpiredError:
            # Let the REST room-start boundary transition the shared auth
            # state and return the stable BILI_AUTH_EXPIRED response.
            raise
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
