# Task 7 + 12 + 16 Evidence

**Verdict**: T7 confirmed, T12 confirmed (flakiness 5/5 stable), T16 satisfied by T4.
**Date**: 2026-06-30

## Commits
- T7: `53f1f46 feat(auth): startup nav check + cookie expiry state machine` (17 tests, 92% cov)
- T12: `f082cb8 feat(room): single-room session + normalized bridge event schema` (11 tests, 94% cov)
- T16: no new commit — satisfied by T4 commit 645e53f (ban/unban/get_ban_list typed + error mapping)

## T7 acceptance (verified)
- check_on_startup: reads settings, calls get_user_info ONLY if cookies present; returns AUTHENTICATED/NEEDS_LOGIN/EXPIRED
- handle_auth_expired: fires callbacks + isolates exceptions (one bad callback doesn't block others)
- require_authenticated: raises NotAuthenticatedError when not AUTHENTICATED (ccShield warn-only anti-pattern fixed)

## T12 acceptance (verified)
- _normalize: DANMU_MSG (info[1]/[2][0]/[2][1]/[7]/[3]/[0][4]) + SUPER_CHAT_MESSAGE; cmd suffix partitioned; malformed→None (no crash)
- single-active-room: connect B stops A first
- _broadcast: isolates callback exceptions
- events.py: Pydantic v2 BaseModel + Literal tags + BridgeEvent closed union

## Flakiness probe (independent verifier, 5 runs each)
- Whole suite: 141 passed ×5 runs (identical counts, exit 0 each)
- test_room_session.py alone: 11 passed ×5 runs
- The transient failure T7-worker observed was NOT reproduced — stable. (Likely first-run artifact.)

## T16 (satisfied by T4)
- T4 verifier confirmed: ban_user/unban_user/get_ban_list in client.py with typed exceptions AuthExpiredError(-101)/PermissionDeniedError(-403)/RateLimitedError(-509).
- get_ban_list running-check deferred to T17 (is_running callback wired, defaults None).

## Integration
- T12 uses T11 DanmakuClient (app/room/session.py:41); T12 ↛ T7 auth (independent); T7 ↛ T12 room (independent). Clean layering.
- Whole suite 141 passed, ruff/basedpyright clean.

## Deferred / risks
- T7: auth_session singleton constructs BilibiliClient at import (httpx client lazy-connects, safe). T8 should drive check_on_startup via lifespan.
- T7: no reentrant guard on check_on_startup vs handle_auth_expired (not relevant for sync startup).
- T12: medal tuple order [name, level] per test fixtures; SC not deduped (tolerable v1).
- T12: _broadcast swallows all exceptions with log (broken callback keeps logging; acceptable).
