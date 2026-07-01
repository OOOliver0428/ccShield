# Task 6 + 11 Evidence — QR auth + danmaku WS client

**Verdict**: T6 confirmed, T11 confirmed (retry after timeout); integration clean.
**Date**: 2026-06-30

## Commits
- T6: `fb3809c feat(auth): qr login with dual-path bili_jct capture, manual fallback, atomic env write` (auth.py 519L, test_auth.py 542L, 22 tests, 91% cov)
- T11: `6980a34 feat(bilibili): danmaku ws client (single-conn) with heartbeat, backoff, watchdog, queue` (danmaku_ws.py 539L, test_danmaku_ws.py 451L, 6 tests, 78% cov)

## T11 retry note
First T11 attempt timed out (30min) on complex multi-conn async mocking, left partial failing test_danmaku_ws.py (8 failures). Respawned simplified (single-conn, FakeWS mock pattern, test seams, 6 tests) — succeeded in 13min, fixed the 8 failures.

## T6 acceptance (verified by independent verifier)
- bili_jct: URL query param FIRST (parse_qs), Set-Cookie fallback, LoginIncompleteError guard if both empty
- write_env_atomic: .tmp + fsync + os.replace
- save_cookies_manual: validates via nav (get_user_info) before persisting
- 22/22 tests pass

## T11 acceptance (verified)
- imports + uses T3 unpack_data/pack_data (4 use sites); NO handwritten parser (grep brace_depth/in_string = none)
- auth failure FATAL (no retry) — _send_auth returns fatal=True on code!=0
- reconnect backoff (1,2,4,8,16,30) × max 6 attempts
- test seams: _heartbeat_interval/_watchdog_timeout/_auth_timeout/_reconnect_delays/_reconnect_max_attempts/_queue_maxsize
- 6/6 tests pass in 0.12s (fully mocked, fast)

## Integration (independent verifier, real exit codes)
- `uv run ruff check .` → 0
- `uv run basedpyright` → 0 errors
- `uv run pytest -v` → 113 passed (zero failures; prior 8 partial-T11 failures gone)
- T11 does NOT import auth.py (independent); uses T3 protocol + T4 bili_client via DI

## Deferred / risks
- T11 multi-connection redundancy deferred (single host_list[0] only; reconnect goes to same URL). Follow-up: host-list fallback.
- T11 `WebSocketLike = object` + `# type: ignore` for ws handle typing (documented).
- T6 save_cookies_manual does NOT close injected httpx client (caller-owned lifecycle).
