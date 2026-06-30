# reccshield-refactor — Planning Draft (resume point)

> Durable, compaction-safe resume point. Prometheus planning session for
> refactoring `~/workspace/ccShield` into `~/workspace/reccshield` (empty target dir).
> Intent routing: **CLEAR + OVERRIDE** (user explicitly requested item-by-item interview;
> adopt-default filter OFF — every surviving fork was ASKED, not defaulted).

- **slug**: reccshield-refactor
- **status**: awaiting-approval
- **pending action**: write `.omo/plans/reccshield-refactor.md` (via scaffold script, after approval)
- **classify**: Architecture (system rebuild, 5 modules)

---

## Interview decisions (all user-confirmed across 5 turns)

### MVP scope (IN — 6 features)
1. **弹幕实时监控** — connect room (short-id resolve), B站 WS live danmaku, display
2. **手动禁言/解禁** — multi-duration ban, view ban-list (WS-pushed), unban
3. **QR扫码Cookie登录** — auto-acquire Cookie: B站 QR login API, user scans with B站 app = consent, tool captures SESSDATA/bili_jct, saves .env. (Replaces ccShield's manual F12 config + the never-finished Cookie Wizard)
4. **SC醒目留言显示** — Super Chat real-time display (data already parsed in protocol layer)
5. **舰队标识** — 总督/提督/舰长 identification (guard_level already parsed)
6. **粉丝牌等级显示** — fan medal level (info[3] already parsed in danmaku_ws.py:427)

### MVP scope (OUT — explicitly excluded)
- **敏感词系统(整个)** — auto-ban AND detection AND CRUD AND stats all removed. No C2 moderation component.
- 三档字体, PyInstaller EXE打包, 礼物/进入消息显示, token认证, Cookie加密存储

### Technical decisions (all user-confirmed)
| Decision | Choice |
|----------|--------|
| 重构策略 | 移植协议IP + 重写其余 (port B站 protocol/WBI/QR logic; rewrite routing/frontend/config/tests) |
| 后端栈 | FastAPI + httpx + websockets (keep) + uv + ruff + basedpyright + Pydantic v2 strict |
| 前端栈 | Vite + TypeScript + Vue3 + Element Plus + Pinia |
| API契约 | FastAPI OpenAPI schema → 生成TS客户端 (structurally eliminates contract mismatch) |
| 禁言列表 | WS推送 (root-fixes ccShield's recurring request-storm pain, LOG_ANALYSIS_FINAL.md) |
| Cookie获取 | QR扫码登录 + .env明文存储 |
| 应用认证 | 单用户本地 + localhost绕过 (no token auth; B站 Cookie is the only credential) |
| 项目布局 | monorepo: backend/ + frontend/ + docs/ |
| 测试架构 | 分层: ①纯逻辑单测TDD(协议帧/WBI/QR URL/去重/状态机,无网络) ②录制fixture契约测试(一次性真机捕获B站响应,离线回放) ③可选live集成测试(@pytest.mark.live默认skip,有Cookie+房间时手动跑). 前端Vitest+MSW |
| 协议解析 | 正规帧解析 (replace danmaku_ws.py:199-244 handwritten JSON brace-matcher) |
| 凭证卫生 | 文档不留密钥 (ccShield leaked real SESSDATA in COOKIE_AUTOBAN_SUMMARY.md:247) |

---

## Components ledger (topology lock — 5 components; C2 moderation REMOVED)

| # | Component | Responsibility | Key refactor vs ccShield |
|---|-----------|----------------|--------------------------|
| C1 | **B站协议层** | danmaku WS protocol (proper frame parsing, port IP) + WBI签名 + B站 HTTP API + **QR扫码登录API(新)** | Port proven protocol/WBI; add QR login; replace handwritten JSON parser; remove dead multi_danmaku_ws.py; fix duplicated bili_client code |
| C2 | **房间编排层** | room lifecycle + message dedup + broadcast + **ban-list WS PUSH(替代轮询)** | Root-fix the recurring Critical request-storm pain |
| C3 | **API/WS网关层** | FastAPI routers (rooms, ban, auth/qr, ws-bridge); OpenAPI schema; localhost bypass | Split god routes.py (753 LOC) into routers/; OpenAPI→TS client; localhost bypass only (no token) |
| C4 | **前端UI层** | Vite+TS+Vue3+ElementPlus+Pinia SPA: danmaku list, SC, 舰队, 粉丝牌, ban controls, QR扫码UI | The "大版本" frontend modernization CHANGELOG deferred; OpenAPI-generated client; kill frontend dup moderation |
| C5 | **基础设施层** | config(.env明文), uv/ruff/basedpyright/Pydantic v2, monorepo scaffold, 分层测试(pytest+vitest+MSW+fixture), CI, credential hygiene | Zero→meaningful test coverage; clean structure |

---

## Key evidence (first-hand verified)

### Scale
- Backend 2,907 LOC/9 files; frontend ~1,828 LOC. God files: routes.py(753), danmaku_ws.py(713), bili_client.py(391). Dead code: multi_danmaku_ws.py(56, deprecated).

### Bugs/debt being fixed by this plan
- Frontend/backend moderation dup (app.js:437-465) → moderation removed entirely from MVP, backend-only if ever re-added.
- API contract mismatch (app.js:50-56 vs routes) → OpenAPI-generated TS client.
- Polling ban-list request storm (LOG_ANALYSIS_FINAL.md) → WS push.
- Duplicated code bili_client.py:310-318,364-367 → cleaned on port.
- Handwritten JSON brace-matcher danmaku_ws.py:199-244 → proper frame parsing.
- Credentials in docs (COOKIE_AUTOBAN_SUMMARY.md:247) → credential hygiene.
- Zero tests → layered test architecture.
- No frontend build → Vite+TS.

### Data already parsed (low-risk display features)
- SC: danmaku_ws.py:451-472 (SUPER_CHAT_MESSAGE). 舰队: guard_level info[7] :425. 粉丝牌: medal info[3] :427. → display-only work in C4.

---

## Approval gate

- **status**: awaiting-approval
- **pending action**: write `.omo/plans/reccshield-refactor.md`
- **approach**: Rebuild ccShield as reccshield monorepo. MVP = 6 features (live danmaku, manual ban/unban, QR Cookie login, SC, fleet, fan-medal). Port B站 protocol/WBI/QR IP, rewrite rest. FastAPI+uv/ruff/basedpyright backend; Vite+TS+Vue3 frontend with OpenAPI-generated client; WS-pushed ban-list; layered tests. Sensitive-word system entirely out of MVP.
- **NOT authorized on approval**: implementation. Approval = write plan file only. Execution begins on user's explicit `$start-work`.
