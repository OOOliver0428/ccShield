# Task 10 + 13 + 17 Evidence — frontend QR + room routes + banlist manager

**Verdict**: T10/T13/T17 confirmed (finisher verified after 1 fix cycle for WS auth path + Any).
**Date**: 2026-06-30

## Commits
- T10: `7ca7090 feat(web): qr login ui + pinia auth store + bootstrap token endpoint`
- T13: `060940f feat(api): room routes + normalized ws bridge`
- T17: `4eaae6e feat(room): banlist manager with snapshot/delta/60s reconcile`
- Fix: `75db63c fix(api): ws token-query auth for /api/ws paths + drop Any from room route tests`

## T10 (frontend QR + bootstrap)
- Bootstrap endpoint GET /api/auth/bootstrap (exempt from token, Host guard still applies) returns LOCAL_TOKEN for localhost — solves chicken-egg.
- Pinia auth store: bootstrap/fetchStatus/startQr/pollQr(2s)/loginManual. QrLogin.vue (QR img + status + 重新生成 + manual fallback). App.vue: bootstrap→fetchStatus→QrLogin|placeholder.
- 11 frontend tests (Vitest+MSW), 22 backend auth tests (incl 2 bootstrap). Frontend typecheck/test/build green.
- Security: LOCAL_TOKEN in JS memory acceptable for single-user local (CSRF/DNS-rebinding defense, not secrecy from localhost).

## T13 (room routes + WS bridge)
- RoomBridge: wraps RoomSession, history deque(maxlen100), WS client registry, broadcasts normalized BridgeEvent.model_dump() (NOT raw B站 frames).
- Routes: GET /rooms/resolve, POST /rooms/start, POST /rooms/stop, GET /rooms, WS /api/ws/rooms/{id}. 14 tests.
- Fix: WS under /api/ws/* now accepts ?token= (browsers can't set Bearer on WebSocket). 5 WS token tests added.

## T17 (banlist manager)
- BanListManager: snapshot on start/subscribe, delta on_ban/on_unban, 60s reconcile (diffs get_ban_list, broadcasts add/remove). _reconcile_interval test seam. 10 tests, 89% cov.
- Catches out-of-band bans (via B站 web UI). is_running callback passed to T4 get_ban_list.

## Combined verification (finisher, real exit codes)
- backend pytest: 200 passed
- frontend typecheck: 0 errors; frontend test: 11 passed; frontend build: success
- make lint: exit 0 (ruff + basedpyright + frontend typecheck all clean)
- Any purged from test_room_routes.py (replaced with proper types)

## Notes / risks
- T13: single global room_bridge (matches single-room model); WS receive_text loop has no idle timeout (slow-peer hardening deferred).
- T13: /rooms/start returns title="" (doesn't call resolve_room_id to avoid coupling; T14+ can populate).
- T17: reconcile silently refreshes changed-ban metadata (no delta for duration changes — spec only mandates add/remove).
- T17: subscribe always emits snapshot (even empty pre-start) — T19 WS store must be idempotent on snapshot.
- WS token in query string may appear in access logs (pre-existing for /ws/*; not a regression).
