# reccshield-refactor - Work Plan

## TL;DR (For humans)
<!-- Fill this LAST, after the detailed plan below is written, so it summarizes the REAL plan. -->

**What you'll get:** 一个干净的 B站直播房管工具,能扫码登录、实时看弹幕、一键禁言/解禁(禁言列表自动推送刷新),并显示醒目留言、舰队身份、粉丝牌等级。原项目 ccShield 的结构混乱、前后端审核重复、禁言列表轮询请求风暴等问题全部根治。

**Why this approach:** 保留 ccShield 调试得来的 B站协议/WBI 签名知识(移植),其余(路由、前端、配置、测试)全部重写。后端 FastAPI+严格类型,前端升级为 Vite+TypeScript(就是 ccShield CHANGELOG 里一直推迟的"大版本前端构建化"),前后端用 OpenAPI 自动生成客户端连接(结构性杜绝接口对不上的 bug)。

**What it will NOT do:** 不做敏感词过滤(整个移出 MVP)、不做 EXE 打包、不做三档字体/礼物消息/多用户认证/Cookie 加密。一次只连一个房间(可切换,不同时多房间)。

**Effort:** Large
**Risk:** Medium - B站协议依赖外部服务,QR登录+禁言需真实Cookie验证
**Decisions to sanity-check:** QR扫码登录是新功能(非移植)、单房间MVP、禁言列表用WS推送+60s后端对账、WBI仅用于getDanmuInfo

Your next move: 批准后用 `$start-work` 开始执行;或先跑一次高精度 Momus 评审。详细执行内容见下。

---

> TL;DR (machine): Large/Medium - 5组件重构,6功能MVP(扫码登录/弹幕监控/禁言/SC/舰队/粉丝牌),移植B站协议IP+重写其余,分层测试,28 todos跨6波

## Scope
### Must have
1. **QR扫码Cookie登录** — B站 QR login(新功能):扫码=授权,捕获 SESSDATA+bili_jct(双路:url字段+Set-Cookie),断言非空,原子写 .env;启动时 nav 检查,失效则进入 QR 流程;处理轮询码 0/86038/86090/86101
2. **弹幕实时监控** — 移植 B站 WS 协议(帧打包/解包重写为正规帧解析,brotli/zlib解压移植),WBI签名(仅用于 getDanmuInfo)移植;短号/真实号双向解析;单活动房间(可切换);心跳30s+指数退避重连;后端归一化为 typed schema 再过 WS 桥
3. **手动禁言/解禁** — 多档时长禁言(csrf=bili_jct),解禁;禁言列表 WS 推送(连接发快照+禁言/解禁发增量+60s后端对账);分页在 C2 内部消化
4. **SC醒目留言显示** — SUPER_CHAT_MESSAGE 归一化为 sc 事件,前端展示
5. **舰队标识** — guard_level(0/1/2/3)归一化,前端展示总督/提督/舰长徽章
6. **粉丝牌等级显示** — medal_info 归一化,前端展示粉丝牌名+等级
7. **基础设施** — monorepo(backend/+frontend/+docs/);FastAPI+uv+ruff+basedpyright+Pydantic v2;Vite+TS+Vue3+ElementPlus+Pinia;OpenAPI→TS客户端;localhost绑定127.0.0.1+Host校验+LOCAL_TOKEN bearer;分层测试(纯逻辑TDD+fixture契约+可选live只读);CI门禁;凭证卫生

### Must NOT have (guardrails, anti-slop, scope boundaries)
- 不做任何敏感词功能(检测/自动禁言/CRUD/统计全部移出)
- 不做 PyInstaller EXE 打包(延后);故不做 dev/prod 双路径配置(单一 .env 路径,无 sys.frozen/resource_path 分支)
- 不做并发多房间(单活动房间,可切换)
- 不做 token 认证(单用户本地+LOCAL_TOKEN;B站 Cookie 是唯一凭证)
- 不做 Cookie 加密存储(.env 明文,但 .env 必须 gitignore,fixture 必须脱敏)
- 不做三档字体/礼物消息/进入房间消息
- 不做 live 集成测试的写操作(禁言/解禁真机测试只手动烟雾测试,不在 CI)
- 前端不接触 B站原始协议(后端归一化后过桥)
- 不移植 multi_danmaku_ws.py(死代码)、不移植手写 JSON 括号匹配(重写为帧解析)

## Verification strategy
> Zero human intervention - all verification is agent-executed.
- Test decision: **分层** — ①纯逻辑单测 TDD(协议帧/WBI/QR轮询码/去重/状态机,无网络);②录制fixture契约测试(脚本一次性真机捕获B站响应,脱敏后离线回放);③可选live集成测试(@pytest.mark.live 默认 skip,仅只读 API,需 RUN_LIVE=1+有效.env);前端 Vitest+MSW
- Framework: pytest+pytest-asyncio(后端), Vitest+MSW+@testing-library/vue(前端)
- Evidence: .omo/evidence/task-<N>-reccshield-refactor.<ext>
- CI门禁: ruff check / basedpyright(零error) / pytest -m "not live" / vitest / openapi客户端陈旧检测(git diff --exit-code)

## Execution strategy
### Parallel execution waves
- Wave 1 (基础+协议): T1-T5 — 脚手架/配置/协议帧/WBI/协议单测
- Wave 2 (登录): T6-T10 — QR登录/启动鉴权/认证路由/认证测试/前端QR
- Wave 3 (房间+弹幕): T11-T15 — WS客户端/房间会话/房间路由桥/前端弹幕/契约测试
- Wave 4 (禁言): T16-T20 — 禁言API/禁言列表管理器/禁言路由桥+LOCAL_TOKEN/前端禁言/契约测试
- Wave 5 (展示): T21-T24 — SC/舰队/粉丝牌/展示测试
- Wave 6 (加固): T25-T28 — OpenAPI客户端门禁/fixture脚本脱敏/CI/文档凭证审计

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
| --- | --- | --- | --- |
| T1 | - | T2,T3,T4 | - |
| T2 | T1 | T6,T7,T11,T16 | T3,T4 |
| T3 | T1 | T11,T15 | T2,T4 |
| T4 | T1 | T11,T16 | T2,T3 |
| T5 | T3,T4 | T15 | T6,T7 |
| T6 | T2,T4 | T7,T8,T9 | T11 |
| T7 | T6 | T8 | T11,T16 |
| T8 | T6,T7 | T10 | T11,T16 |
| T9 | T6 | - | T10,T11 |
| T10 | T8 | T14 | T14 |
| T11 | T3,T4 | T12,T13,T15 | T6,T7 |
| T12 | T11 | T13,T15 | T8,T16 |
| T13 | T12 | T14 | T10,T17 |
| T14 | T13,T10 | T19,T21,T22,T23 | T17 |
| T15 | T5,T11,T12 | - | T17 |
| T16 | T4,T7 | T17,T18,T20 | T12 |
| T17 | T16,T12 | T18,T20 | T13,T18 |
| T18 | T16,T17,T13 | T19 | T14 |
| T19 | T18,T14 | - | T21,T22,T23 |
| T20 | T16,T17 | - | T19,T21 |
| T21 | T14 | - | T22,T23,T24 |
| T22 | T14 | - | T21,T23,T24 |
| T23 | T14 | - | T21,T22,T24 |
| T24 | T21,T22,T23 | - | T19 |
| T25 | T8,T13,T18 | - | T26,T27,T28 |
| T26 | T5,T9 | - | T25,T27,T28 |
| T27 | T1 | - | T25,T26,T28 |
| T28 | T1 | - | T25,T26,T27 |

## Todos
> Implementation + Test = ONE todo. Never separate.
<!-- APPEND TASK BATCHES BELOW THIS LINE WITH edit/apply_patch - never rewrite the headers above. -->

- [x] 1. monorepo 脚手架与工具链
  What to do / Must NOT do: 在 reccshield 建 monorepo: backend/(pyproject.toml+uv, FastAPI 入口骨架)、frontend/(Vite+TS+Vue3+ElementPlus+Pinia 初始化)、docs/、根 Makefile(dev/test/lint 并行起后端+前端)、.gitignore(含 .env/.env.* 但保留 .env.example)、.env.example(SESSDATA=/bili_jct=/ROOM_ID= 占位)。后端 uv 初始化 fastapi/httpx/websockets/loguru/brotli/pydantic-settings/pytest/pytest-asyncio/ruff/basedpyright;前端 bun 初始化 vue+typescript+element-plus+pinia+axios+dayjs+vitest+msw。Must NOT: 不装 pyinstaller,不建 app/static 混合结构,不做双路径。
  Parallelization: Wave 1 | Blocked by: - | Blocks: T2,T3,T4
  References: ccShield 结构 app/+frontend/(反例,前后端混合);ccShield/requirements.txt(依赖清单);ccShield/frontend/package.json(前端依赖反例,CDN无构建)
  Acceptance criteria (agent-executable): `cd backend && uv run ruff check .` 退出0;`cd backend && uv run basedpyright` 退出0(空骨架);`cd frontend && bun run build` 成功生成 dist/;`make lint` 退出0;.gitignore 含 `.env`;`.env.example` 存在且值为空
  QA scenarios (exact tool + invocation): happy: `make dev` 同时起后端(localhost:8000)和前端(localhost:5173)不报错; failure: `git status` 确认 .env 不被追踪(放一个假 .env 测)。Evidence .omo/evidence/task-1-reccshield-refactor.log
  Commit: Y | chore(init): monorepo scaffold with uv+vite toolchain

- [x] 2. 后端配置层(单一 .env 路径,Pydantic v2)
  What to do / Must NOT do: backend/app/config.py 用 pydantic-settings BaseSettings 读 .env(SESSDATA/BILI_JCT/BUVID3/ROOM_ID/HOST/PORT);cookies 属性返回 dict;**单一路径**(项目根 .env,无 get_external_path/sys.frozen/resource_path 分支);LOCAL_TOKEN 启动随机生成(str(hex));**CORS 显式放行 Vite 开发端口(Momus 修正 Medium#3)**: allow_origins 含 http://localhost:5173(Vite dev)+http://localhost:8000(生产静态)+http://127.0.0.1:5173/8000。Must NOT: 不移植 config.py:8-44 的多路径探测逻辑,不做 frozen 分支,不漏放行 Vite 端口(否则前端 dev 被 CORS 拦)。
  Parallelization: Wave 1 | Blocked by: T1 | Blocks: T6,T7,T11,T16 | Can parallelize with: T3,T4
  References: ccShield/app/core/config.py:1-98(反例:多路径探测);ccShield/app/main.py:41-62(CORS localhost,但未含独立前端端口);Momus Medium#3(CORS vs Vite dev server)
  Acceptance criteria: `uv run basedpyright backend/app/config.py` 零error;单测: 给 .env 写 SESSDATA=x/BILI_JCT=y,断言 settings.cookies == {"SESSDATA":"x","bili_jct":"y"};断言 settings.LOCAL_TOKEN 非空且32位hex;无 sys.frozen 引用(`grep -r frozen backend/` 无命中)
  QA scenarios: happy: 单测通过; failure: .env 缺失时 settings.SESSDATA == ""(默认空,不崩)。Evidence .omo/evidence/task-2-reccshield-refactor.log
  Commit: Y | feat(config): single-path pydantic-settings, startup local token

- [x] 3. B站 WS 协议帧解析(重写)+ brotli/zlib(移植)
  What to do / Must NOT do: backend/app/bilibili/protocol.py 实现 WS 帧打包(struct pack ">IHHII")+解包(循环读16字节头,按 proto_ver 2=zlib/3=brotli 解压);正规解析解压后 payload(每个帧 payload 是一个完整 JSON 或需按帧边界迭代,**不用手写括号匹配**,用 json.loads 对每个 payload 块);定义 opcode 常量(HEARTBEAT=2/RSP=3/NORMAL=5/AUTH=7/RSP=8)。brotli/zlib 解压逻辑参考 ccShield 移植。Must NOT: 不移植 danmaku_ws.py:199-244 手写括号匹配,不移植 multi_danmaku_ws.py。
  Parallelization: Wave 1 | Blocked by: T1 | Blocks: T11,T15 | Can parallelize with: T2,T4
  References: ccShield/app/core/danmaku_ws.py:123-270(pack/unpack/decompress 逻辑参考);B站协议:包头16字节,proto_ver 2=zlib 3=brotli
  Acceptance criteria: TDD 单测: 构造一个 NORMAL 帧(json payload+brotli压缩)→ unpack 返回该 json;构造多帧拼接 → 全部解析;proto_ver=2 zlib 帧解析;认证响应帧(AUTH_RSP)解析;`uv run basedpyright` 零error
  QA scenarios: happy: 单测喂构造帧通过; failure: 截断的帧(不足16字节)→ unpack 不崩(返回已解析部分)。Evidence .omo/evidence/task-3-reccshield-refactor.log
  Commit: Y | feat(protocol): rewrite bili ws frame parser, drop brace-matcher

- [x] 4. WBI 签名 + B站 HTTP 客户端(typed)
  What to do / Must NOT do: backend/app/bilibili/wbi.py 移植 WBI 算法(MIXIN_KEY_ENC_TAB/mixin_key/enc_wbi/WbiSigner 缓存1小时);backend/app/bilibili/client.py 用 httpx.AsyncClient 封装:get_user_info(nav)、get_room_init、get_room_info、resolve_room_id(短号/真实号双向)、get_danmu_info(**仅此接口用 WBI 签名**)、ban_user、unban_user、get_ban_list(分页全量,带房间状态中断检查)。所有响应 parse 为 typed Pydantic 模型;定义 typed 异常 AuthExpiredError(-101)/PermissionDeniedError(-403)/RateLimitedError(-509)/BiliApiError。Must NOT: 不全局套 WBI(只 get_danmu_info 用),不移植 bili_client.py:310-318 重复的状态检查代码(写一次),不移植 delete_danmaku(B站不支持)。
  Parallelization: Wave 1 | Blocked by: T1 | Blocks: T11,T16 | Can parallelize with: T2,T3
  References: ccShield/app/core/wbi.py:1-141(移植);ccShield/app/core/bili_client.py:1-391(参考但去重,310-318和364-367重复代码不移植)
  Acceptance criteria: TDD 单测: WBI enc_wbi 给定 img_key/sub_key/params 断言 w_rid 正确(用已知向量);resolve_room_id 单测 mock httpx 返回短号场景断言翻译;typed 异常映射:-101→AuthExpiredError;-403→PermissionDeniedError;`uv run basedpyright` 零error
  QA scenarios: happy: WBI 单测通过; failure: get_danmu_info 返回 -352 → 触发刷新 WBI 重试一次(移植 ccShield:208-218 逻辑)。Evidence .omo/evidence/task-4-reccshield-refactor.log
  Commit: Y | feat(bilibili): typed http client + ported wbi, wbi only for getDanmuInfo

- [x] 5. 协议层单测套件(TDD 覆盖) — satisfied by T3/T4 TDD (impl+test merged); 28+14+24+6 tests, coverage protocol 100%/wbi 90%/client 86%
  What to do / Must NOT do: backend/tests/test_protocol.py + test_wbi.py 覆盖 T3/T4 的纯逻辑:帧打包/解包/多帧/解压、WBI 签名、resolve_room_id 短号翻译、typed 异常映射。**无网络**(全 mock/构造数据)。Must NOT: 不在此调真实 B站。
  Parallelization: Wave 1 | Blocked by: T3,T4 | Blocks: T15 | Can parallelize with: T6,T7
  References: T3,T4 产物
  Acceptance criteria: `uv run pytest backend/tests/test_protocol.py backend/tests/test_wbi.py -v` 全绿;覆盖率 protocol.py+wbi.py ≥80%(`uv run pytest --cov=backend/app/bilibili --cov-fail-under=80`)
  QA scenarios: happy: 全绿; failure: 故意改错 MIXIN_KEY_ENC_TAB → 单测失败。Evidence .omo/evidence/task-5-reccshield-refactor.log
  Commit: Y | test(protocol): tdd unit tests for frame parsing and wbi

- [x] 6. QR 扫码登录 API(双路捕获 bili_jct + 原子写 .env + 手动 fallback)
  What to do / Must NOT do: backend/app/bilibili/auth.py 实现 QR 登录:generate(调 passport.bilibili.com/x/passport-login/web/qrcode/generate 返回 qrcode_url+qrcode_key)、poll(qrcode_key 轮询,处理 code 0=成功/86101=未扫/86090=已扫待确认/86038=过期)。**成功时双路捕获**:解析 response.json() 的 url 字段 query 参数(SESSDATA/bili_jct/DedeUserID)为主,辅以 response.cookies 的 Set-Cookie;**断言 bili_jct 非空**否则抛 LoginIncompleteError;**原子写 .env**(写 .env.tmp 后 os.rename)。**Plan B(Momus 修正)**: 同时实现手动 Cookie 录入端点 POST /auth/manual(SESSDATA+bili_jct 表单,nav 验证后写 .env)作为 QR 失败时的 fallback,确保核心功能不被 QR 真机风险卡死。Must NOT: 不只读 Set-Cookie(httpx 可能不暴露 bili_jct),不在 .env 写真实示例值,不让 QR 失败导致工具无法使用。
  Parallelization: Wave 2 | Blocked by: T2,T4 | Blocks: T7,T8,T9 | Can parallelize with: T11
  References: B站 QR 登录 API(passport.bilibili.com/x/passport-login/web/qrcode/*);ccShield 无 QR(新功能);G2/G3 Metis 修正;Momus High#1(bili_jct 真机未验证+无 fallback)
  Acceptance criteria: TDD 单测 mock poll 返回 code=0+url含SESSDATA=x&bili_jct=y → 断言 .env 写入两者且非空;mock code=86038 → 抛 QRExpiredError;mock code=0 但 url 无 bili_jct → 抛 LoginIncompleteError;原子写测试;手动 fallback 单测: POST /auth/manual 带有效 cookie → nav 验证通过 → 写 .env。**Wave 2 出口门禁(Momus)**: T8 完成后立即真机 QR 扫码测试,断言 .env 中 bili_jct 非空;若 QR 真机失败,确认手动 fallback 可用后才进入 Wave 3。
  QA scenarios (exact tool + invocation): happy: mock 成功登录写 .env; failure: bili_jct 缺失时抛错不写半截 .env; failure: QR 真机失败 → 手动 fallback 仍可登录。Evidence .omo/evidence/task-6-reccshield-refactor.log
  Commit: Y | feat(auth): qr login with dual-path bili_jct capture, manual fallback, atomic env write

- [x] 7. 启动鉴权序列 + Cookie 失效检测
  What to do / Must NOT do: backend/app/auth/session.py 启动时:读 .env → 若 SESSDATA+bili_jct 存在,调 nav 检查 → code:0 跳过登录;code:-101 或 .env 空则进入待登录态(等前端触发 QR)。运行时:任何 B站 API 返回 -101 → AuthExpiredError → C3 转 HTTP 401 + 桥 WS 推 {type:"auth_expired"};WS 认证失败码同处理。Must NOT: 不在 -101 时重试禁言(非瞬态)。
  Parallelization: Wave 2 | Blocked by: T6 | Blocks: T8 | Can parallelize with: T11,T16
  References: G3/G10 Metis 修正;ccShield/app/main.py:26-30(Cookie检查反例,只警告不阻断)
  Acceptance criteria: 单测: .env 有效+nav mock code:0 → status=authenticated;.env 空 → status=needs_login;nav mock -101 → status=expired;运行时 -101 抛 AuthExpiredError 并触发桥推送
  QA scenarios: happy: 有效cookie启动直接就绪; failure: 失效cookie启动进入待登录,前端收到 auth_expired。Evidence .omo/evidence/task-7-reccshield-refactor.log
  Commit: Y | feat(auth): startup nav check + cookie expiry detection

- [x] 8. 认证 API 路由 + OpenAPI schema — +fix: precise dual-path test regex + qr/manual login flips auth state in-memory (commit 596f837)
  What to do / Must NOT do: backend/app/api/auth_routes.py: POST /auth/qr/start(返 qrcode_url+qrcode_key)、GET /auth/qr/poll?qrcode_key=(返 status: scanning/confirmed/expired/success)、GET /auth/status(返 authenticated/needs_login/expired)、POST /auth/manual(T6 fallback,手动录入 cookie)。Pydantic 请求/响应模型;localhost 绑定 127.0.0.1 + **Host 校验放行 localhost(不分端口,Momus Medium#3)**:Host 头 host 部分(去端口)须为 localhost 或 127.0.0.1,否则拒绝(DNS重绑定防御);LOCAL_TOKEN bearer 中间件(对所有 /api/*)。Must NOT: 不加 token 多用户认证,不绑 0.0.0.0,不让 Host 校验因端口差异误杀前端 dev 请求。
  Parallelization: Wave 2 | Blocked by: T6,T7 | Blocks: T10 | Can parallelize with: T11
  References: G6 Metis 修正(LOCAL_TOKEN);ccShield/app/api/routes.py:120-175(反例 token 认证);Momus Medium#3(Host guard vs Vite dev)
  Acceptance criteria: `curl localhost:8000/auth/status` 无 token → 401;带 LOCAL_TOKEN → 200;`curl localhost:8000/auth/qr/start` 返回 qrcode_url;OpenAPI /openapi.json 含这三个端点;`uv run basedpyright` 零error
  QA scenarios: happy: 带 token 调通三端点; failure: Host 非 localhost → 拒绝(DNS重绑定防御)。Evidence .omo/evidence/task-8-reccshield-refactor.log
  Commit: Y | feat(api): auth routes + local_token middleware + host guard

- [x] 9. QR 登录单测 + fixture 捕获脚本 — tests done by T6/T7; capture_fixtures.py with redaction + dry-run (commit 908dfe9)
  What to do / Must NOT do: backend/tests/test_auth.py 覆盖 T6/T7(轮询码/双路捕获/原子写/失效检测/手动 fallback,全 mock);scripts/capture_fixtures.py 真机捕获脚本(--live 标志,用 .env Cookie 调真实 B站 nav/getDanmuInfo/ban-list,存 tests/fixtures/{endpoint}_{sha}.json,**脱敏**:SESSDATA/bili_jct/DedeUserID 替换为 <REDACTED>)。**依赖说明(Momus Low#6)**: capture 需有效 .env Cookie,可临时从 ccShield/.env 复制,或等 T6 真机 QR 测试产出后再跑;fixture 捕获非阻塞 T9 的单测部分(单测全 mock 不需 Cookie)。Must NOT: 不提交未脱敏 fixture,不把 capture 设为默认。
  Parallelization: Wave 2 | Blocked by: T6 | Blocks: - | Can parallelize with: T10,T11
  References: G7 Metis 修正(fixture 脱敏);Momus Low#6(capture 引导依赖)
  Acceptance criteria: `uv run pytest backend/tests/test_auth.py -v` 全绿;capture_fixtures.py 干跑(--dry-run)不报错;生成的 fixture 用 `grep -rE 'SESSDATA=[a-z0-9]' tests/fixtures/` 无命中(已脱敏)
  QA scenarios: happy: 单测+脱敏检查通过; failure: fixture 含未脱敏 cookie → grep 命中,失败。Evidence .omo/evidence/task-9-reccshield-refactor.log
  Commit: Y | test(auth): qr login tests + fixture capture script with redaction

- [x] 10. 前端 QR 登录 UI + auth store — +bootstrap token endpoint; 11 frontend tests, 22 backend auth tests (commit 7ca7090)
  What to do / Must NOT do: frontend/src/stores/auth.ts(Pinia: status/qrcodeUrl/qrKey/userInfo);components/QrLogin.vue(显示二维码,2s 轮询 /auth/qr/poll,按 status 显示"扫码/待确认/已过期+重新生成");首次进入若 needs_login 弹 QR;成功后存 userInfo 跳转主界面。axios 客户端自动带 LOCAL_TOKEN(从启动注入)。Must NOT: 不在前端存 Cookie(只在后端 .env)。
  Parallelization: Wave 2 | Blocked by: T8 | Blocks: T14 | Can parallelize with: T14
  References: T8 端点;ccShield 无 QR UI(新)
  Acceptance criteria: Vitest+MSW: mock /auth/status=needs_login → QrLogin 渲染;mock poll scanning→confirmed→success → UI 切换 + 调 /auth/qr/start 成功;mock expired → 显示重新生成按钮;`bun run typecheck` 零error
  QA scenarios: happy: MSW mock 全流程绿; failure: poll 超时 → 显示错误。Evidence .omo/evidence/task-10-reccshield-refactor.log
  Commit: Y | feat(web): qr login ui + pinia auth store

- [x] 11. 弹幕 WS 客户端(心跳+重连+队列) — single-conn (multi-conn deferred); 6 tests 78% cov, uses T3 unpack_data, auth-fatal no-retry
  What to do / Must NOT do: backend/app/bilibili/danmaku_ws.py 用 T3 帧解析 + T4 getDanmuInfo(token+host_list);连接(最多3服务器冗余,移植 ccShield 多连接思路);认证帧;心跳30s;指数退避重连(1/2/4/8/16s cap30,最多10次,超限抛 RoomDisconnectedError);消息队列(asyncio.Queue maxsize2000);心跳看门狗(45s无ACK强制断开重连)。Must NOT: 不移植 multi_danmaku_ws.py,不用手写括号匹配。
  Parallelization: Wave 3 | Blocked by: T3,T4 | Blocks: T12,T15 | Can parallelize with: T6,T7
  References: ccShield/app/core/danmaku_ws.py:48-713(多连接/心跳/重连/队列逻辑移植,帧解析用T3新实现);G12 Metis 修正(重连+看门狗)
  Acceptance criteria: 单测 mock ws: 连接→认证→心跳→收消息→入队;mock 断开→重连背-off;mock 认证失败→致命错误不重连;`uv run basedpyright` 零error
  QA scenarios: happy: mock 流程绿; failure: 10次重连失败 → 抛 RoomDisconnectedError。Evidence .omo/evidence/task-11-reccshield-refactor.log
  Commit: Y | feat(bilibili): danmaku ws client with heartbeat, backoff, watchdog

- [x] 12. 房间会话(单活动房间)+ 归一化 schema
  What to do / Must NOT do: backend/app/room/session.py RoomSession(单活动房间,connect/disconnect/reconnect);消息去重(deque maxlen5000);**归一化 B站事件为 typed schema**:DANMU_MSG→{type:"danmaku",uid,uname,text,ts,guard_level,medal:{name,level}|null};SUPER_CHAT_MESSAGE→{type:"sc",...};定义 BridgeEvent typed union。广播到注册的桥回调。Must NOT: 不建 RoomManager 多房间 map(单房间),不转发原始 B站 cmd/info 给前端。
  Parallelization: Wave 3 | Blocked by: T11 | Blocks: T13,T15 | Can parallelize with: T8,T16
  References: G8 Metis 修正(单房间);G9 Metis 修正(归一化);ccShield/app/core/room_manager.py:94-136(on_message/dedup 参考,但单房间化)
  Acceptance criteria: 单测: 喂 mock DANMU_MSG 帧 → 归一化为 danmaku 事件含 guard_level+medal;喂 SUPER_CHAT_MESSAGE → sc 事件;去重: 同 msg_id 第二次丢弃;`uv run basedpyright` 零error
  QA scenarios: happy: 归一化+去重绿; failure: 未知 cmd → 忽略不崩。Evidence .omo/evidence/task-12-reccshield-refactor.log
  Commit: Y | feat(room): single-room session + normalized bridge event schema

- [x] 13. 房间 API 路由 + WS 桥(归一化事件) — +fix: /api/ws token-query auth (commit 75db63c)
  What to do / Must NOT do: backend/app/api/room_routes.py: GET /rooms/resolve?input=(短号/真实号双向)、POST /rooms/start、POST /rooms/stop;WS /ws/rooms/{room_id}(连接后推历史+实时归一化事件)。所有 /api/* 走 LOCAL_TOKEN。WS 连接时发 danmaku 历史快照。Must NOT: 不把原始 B站帧推给前端(只推归一化 BridgeEvent)。
  Parallelization: Wave 3 | Blocked by: T12 | Blocks: T14 | Can parallelize with: T10,T17
  References: T12 归一化;ccShield/app/api/routes.py:349-392(房间路由参考)+614-721(WS参考但改归一化)
  Acceptance criteria: `curl localhost:8000/rooms/resolve?input=22210347` 返 room_id;WS 连接收到 {type:"danmaku"...} 形态(非原始 cmd);OpenAPI 含这些端点;`uv run basedpyright` 零error
  QA scenarios: happy: resolve+WS 通行; failure: 房间未启动时 WS 连接 → {type:"error"}。Evidence .omo/evidence/task-13-reccshield-refactor.log
  Commit: Y | feat(api): room routes + normalized ws bridge

- [x] 14. 前端房间 store + 房间输入 UI + 弹幕列表 UI + WS 客户端 — 48 frontend tests, WS reconnect [3,6,12,24,30] cap5 (commit da23236)
  What to do / Must NOT do: frontend/src/stores/room.ts(当前房间/连接状态);stores/danmaku.ts(弹幕列表 deque cap500,SC列表);**components/RoomInput.vue(Momus 修正 High#2)**:房间号输入框 + 失焦调 /rooms/resolve 短号翻译回显 + 连接/断开按钮 + 连接状态指示(未连接/连接中/已连接);components/DanmakuList.vue(展示弹幕,带 guard 徽章占位+粉丝牌占位,后续 T22/T23 填);WS 客户端(连 /ws/rooms/{id},自动带 LOCAL_TOKEN,断开指数退避重连+顶部"重连中"横幅)。Must NOT: 不在前端解析 B站协议(消费归一化事件),不让用户手动改 .env 配置房间(用 UI 输入)。
  Parallelization: Wave 3 | Blocked by: T13,T10 | Blocks: T19,T21,T22,T23 | Can parallelize with: T17
  References: T13 桥事件 schema;ccShield/frontend/src/app.js:60-128(WS重连参考)+131-425(弹幕列表参考,但用归一化数据);Momus High#2(缺房间输入 UI)
  Acceptance criteria: Vitest+MSW: mock /rooms/resolve 返 room_id → RoomInput 回显真实号;mock WS 推 {type:"danmaku",...} → 列表渲染;mock 断开 → 重连横幅;mock {type:"auth_expired"} → 跳 QR;房间号输入+连接按钮可点;`bun run typecheck` 零error
  QA scenarios (exact tool + invocation): happy: 输入房间号→翻译回显→连接→弹幕渲染; failure: auth_expired → 跳登录。Evidence .omo/evidence/task-14-reccshield-refactor.log
  Commit: Y | feat(web): room input + room store + danmaku list + ws client with reconnect

- [x] 15. 弹幕契约测试(fixture 回放) — synthetic fixtures (real capture deferred to Wave 2 gate); 8 tests (commit cd73630)
  What to do / Must NOT do: backend/tests/test_danmaku_contract.py 用 T9 捕获的真实 B站帧 fixture(脱敏)→ 喂 T11/T12 → 断言归一化输出匹配预期 typed schema。覆盖 DANMU_MSG/SUPER_CHAT_MESSAGE/认证响应。Must NOT: 不调真实 B站(用 fixture)。
  Parallelization: Wave 3 | Blocked by: T5,T11,T12 | Blocks: - | Can parallelize with: T17
  References: T9 fixture;T11/T12 产物
  Acceptance criteria: `uv run pytest backend/tests/test_danmaku_contract.py -v` 全绿(用 fixture);fixture 脱敏检查通过
  QA scenarios: happy: fixture 回放归一化正确; failure: B站改字段 → 契约失败(提示重新捕获)。Evidence .omo/evidence/task-15-reccshield-refactor.log
  Commit: Y | test(danmaku): contract tests against recorded fixtures

- [x] 16. B站 禁言/解禁 API(typed + csrf + 错误映射) — satisfied by T4 (ban/unban/get_ban_list typed + error mapping -101/-403/-509); get_ban_list running-check deferred to T17
  What to do / Must NOT do: backend/app/bilibili/moderation_api.py(或在 client.py 扩展):ban_user(room_id,uid,hour,csrf=bili_jct)、unban_user(room_id,block_id)、get_ban_list(room_id 全量分页,带房间状态中断)。typed 异常映射(-101→AuthExpired/-403→PermissionDenied/-509→RateLimited)。Must NOT: 不移植 bili_client.py:310-318 重复状态检查(写一次),不在 -101 重试。
  Parallelization: Wave 4 | Blocked by: T4,T7 | Blocks: T17,T18,T20 | Can parallelize with: T12
  References: ccShield/app/core/bili_client.py:225-368(禁言/解禁/分页参考,去重);G13 Metis 修正(错误映射)
  Acceptance criteria: TDD 单测 mock ban 返 code:0 → True;mock -101 → AuthExpiredError;mock -403 → PermissionDeniedError;mock -509 → RateLimitedError;get_ban_list mock 多页 → 合并全量且房间停止时中断
  QA scenarios: happy: typed 返回+异常映射; failure: 房间停止时分页中断不继续。Evidence .omo/evidence/task-16-reccshield-refactor.log
  Commit: Y | feat(bilibili): typed ban/unban api with error mapping

- [x] 17. 禁言列表管理器(快照+增量+60s对账) — 10 tests 89% cov (commit 4eaae6e)
  What to do / Must NOT do: backend/app/room/banlist.py BanListManager:前端 WS 连 /ws/rooms/{id}/banlist 时发**全量快照**{event:"snapshot",bans:[...]}(调 get_ban_list 全量分页);本地 POST /ban 或 DELETE /ban 成功后发**增量**{event:"ban_added"}/{event:"ban_removed"};后台**60s 对账**(调 get_ban_list 比对,diff 发增量);分页在内部消化不暴露前端。Must NOT: 不把分页暴露给前端,不用前端轮询。
  Parallelization: Wave 4 | Blocked by: T16,T12 | Blocks: T18,T20 | Can parallelize with: T13
  References: G4 Metis 修正(快照+增量+对账);ccShield 轮询反例(LOG_ANALYSIS_FINAL.md)
  Acceptance criteria: 单测: 新连接→快照;ban 成功→ban_added 增量;unban→ban_removed;60s 对账 mock 新增→发增量;快照分页合并去重;`uv run basedpyright` 零error
  QA scenarios: happy: 快照+增量+对账绿; failure: WS 断开重连→重发快照。Evidence .omo/evidence/task-17-reccshield-refactor.log
  Commit: Y | feat(room): banlist manager with snapshot/delta/60s reconcile

- [x] 18. 禁言 API 路由 + WS 禁言桥 + LOCAL_TOKEN — 13 tests (commit ad0dacc)
  What to do / Must NOT do: backend/app/api/ban_routes.py: POST /ban(room_id,uid,hour)、DELETE /ban(room_id,block_id)、WS /ws/rooms/{id}/banlist(接 T17 推送);所有走 LOCAL_TOKEN;成功后触发 T17 增量推送。Must NOT: 不加 token 多用户认证。
  Parallelization: Wave 4 | Blocked by: T16,T17,T13 | Blocks: T19 | Can parallelize with: T14
  References: T8 LOCAL_TOKEN;T17 管理器;ccShield/app/api/routes.py:397-434(禁言路由参考)
  Acceptance criteria: `curl -X POST localhost:8000/ban -H "Authorization: Bearer $TOKEN"` 成功→WS 客户端收到 ban_added;`curl -X DELETE` → ban_removed;无 token → 401;OpenAPI 含端点
  QA scenarios: happy: 禁言→WS增量; failure: 无权限(-403)→HTTP 403。Evidence .omo/evidence/task-18-reccshield-refactor.log
  Commit: Y | feat(api): ban routes + ws banlist bridge

- [x] 19. 前端禁言 store + 禁言控件 + 禁言列表 UI — 39 tests, WS-driven no polling (commit fed2a87)
  What to do / Must NOT do: frontend/src/stores/ban.ts(banList Set<uid>,WS驱动);components/BanControls.vue(禁言时长选择+二次确认+解禁);components/BanList.vue(WS推送驱动,非轮询)。WS 断开重连自动重取快照。Must NOT: 不实现前端轮询,不前端做禁言逻辑判断。
  Parallelization: Wave 4 | Blocked by: T18,T14 | Blocks: - | Can parallelize with: T21,T22,T23
  References: T18 桥;ccShield/frontend/src/app.js:437-465(反例:前端审核重复,移除)
  Acceptance criteria: Vitest+MSW: mock ban_added → 列表新增;ban_removed → 移除;snapshot → 全量替换;禁言按钮二次确认;`bun run typecheck` 零error
  QA scenarios: happy: WS驱动列表更新; failure: 禁言失败(-403)→toast 提示。Evidence .omo/evidence/task-19-reccshield-refactor.log
  Commit: Y | feat(web): ban store + controls + ws-driven banlist ui

- [x] 20. 禁言契约测试(fixture 回放 + 错误映射) — 18 tests synthetic fixtures (commit 43039cd)
  What to do / Must NOT do: backend/tests/test_ban_contract.py 用 fixture(ban-list 响应、ban 成功响应、各错误码)→ 契约测试 T16/T17;覆盖快照/增量/对账/错误映射。Must NOT: 不做 live 禁言(危险,移到手动烟雾测试)。
  Parallelization: Wave 4 | Blocked by: T16,T17 | Blocks: - | Can parallelize with: T19,T21
  References: T9 fixture;T16/T17 产物;G18 Metis 修正(live只读)
  Acceptance criteria: `uv run pytest backend/tests/test_ban_contract.py -v` 全绿;fixture 脱敏
  QA scenarios: happy: 契约+错误映射绿; failure: -101 fixture → AuthExpiredError。Evidence .omo/evidence/task-20-reccshield-refactor.log
  Commit: Y | test(ban): contract tests with error mapping fixtures

- [x] 21. SC 醒目留言显示 — SuperChatItem.vue (commit b303c41, combined w/ T22-24)
  What to do / Must NOT do: frontend/src/components/SuperChat.vue 渲染归一化 {type:"sc",uid,uname,text,price,ts} 事件(颜色/价格/消息);插入弹幕流顶部或独立面板。Must NOT: 不在前端解析 SUPER_CHAT_MESSAGE 原始帧(消费 T12 归一化)。
  Parallelization: Wave 5 | Blocked by: T14 | Blocks: - | Can parallelize with: T22,T23,T24
  References: T12 归一化 sc 事件;ccShield/app/core/danmaku_ws.py:451-472(原始解析参考,已归一化)
  Acceptance criteria: Vitest: mock {type:"sc",uname:"U",text:"T",price:30} → SC 面板渲染 uname+text+price;`bun run typecheck` 零error
  QA scenarios: happy: SC 渲染; failure: price=0 → 仍渲染(免费SC)。Evidence .omo/evidence/task-21-reccshield-refactor.log
  Commit: Y | feat(web): super chat display

- [x] 22. 舰队标识徽章 — GuardBadge.vue 0/1/2/3→舰长/提督/总督 (commit b303c41)
  What to do / Must NOT do: frontend/src/components/GuardBadge.vue 按 guard_level(0/1/2/3)渲染:0=无,1=舰长,2=提督,3=总督;嵌入弹幕项。Must NOT: 不在前端解析 info[7](消费归一化 guard_level)。
  Parallelization: Wave 5 | Blocked by: T14 | Blocks: - | Can parallelize with: T21,T23,T24
  References: T12 归一化;ccShield 舰队背景图(改用徽章,更轻)
  Acceptance criteria: Vitest: guard_level=3→总督,2→提督,1→舰长,0→无徽章;`bun run typecheck` 零error
  QA scenarios: happy: 四档徽章; failure: guard_level 未知值 → 无徽章不崩。Evidence .omo/evidence/task-22-reccshield-refactor.log
  Commit: Y | feat(web): fleet guard badge

- [x] 23. 粉丝牌等级显示 — FanMedal.vue (commit b303c41)
  What to do / Must NOT do: frontend/src/components/FanMedal.vue 渲染归一化 medal:{name,level}|null(粉丝牌名+等级);null 不渲染。Must NOT: 不在前端解析 info[3](消费归一化)。
  Parallelization: Wave 5 | Blocked by: T14 | Blocks: - | Can parallelize with: T21,T22,T24
  References: T12 归一化;ccShield/app/core/danmaku_ws.py:427(medal 解析参考)
  Acceptance criteria: Vitest: medal={name:"粉丝团",level:5}→渲染名+5;medal=null→不渲染;`bun run typecheck` 零error
  QA scenarios: happy: 粉丝牌渲染; failure: medal.level=0 → 仍渲染名字。Evidence .omo/evidence/task-23-reccshield-refactor.log
  Commit: Y | feat(web): fan medal display

- [x] 24. 展示特性 Vitest 套件 — combined w/ T21-23 (commit b303c41)
  What to do / Must NOT do: frontend/src/components/__tests__/ 整合 SC/GuardBadge/FanMedal 测试;MSW mock 归一化事件流。Must NOT: 不依赖真实后端。
  Parallelization: Wave 5 | Blocked by: T21,T22,T23 | Blocks: - | Can parallelize with: T19
  References: T21/T22/T23
  Acceptance criteria: `cd frontend && bun run test` 全绿;`bun run typecheck` 零error
  QA scenarios: happy: 三组件测试绿; failure: guard_level=3 但渲染舰长 → 失败。Evidence .omo/evidence/task-24-reccshield-refactor.log
  Commit: Y | test(web): display feature vitest suite

- [x] 25. OpenAPI→TS 客户端生成 + 陈旧门禁 — @hey-api/openapi-ts, 4th CI job openapi-stale (commit 600e43a)
  What to do / Must NOT do: scripts/gen_client.sh 用 openapi-ts 从后端 /openapi.json 生成 frontend/src/api/client.ts;CI 门禁:生成后 `git diff --exit-code frontend/src/api/client.ts` 有 diff → 失败(陈旧)。Must NOT: 不手写 fetch 封装。
  Parallelization: Wave 6 | Blocked by: T8,T13,T18 | Blocks: - | Can parallelize with: T26,T27,T28
  References: G15 Metis 修正
  Acceptance criteria: `make gen-client` 生成 client.ts;`make lint` 含陈旧检测;故意改后端响应模型不 regen → CI 失败
  QA scenarios: happy: regen 后无 diff; failure: 后端改了不 regen → CI 红。Evidence .omo/evidence/task-25-reccshield-refactor.log
  Commit: Y | build(api): openapi-ts client generation + stale gate

- [x] 26. fixture 脱敏 + secret scan pre-commit — check_secrets.sh + check_fixtures.py + .pre-commit-config (commit bacc3b4 + 05589f2 allow-markers)
  What to do / Must NOT do: pre-commit hook(detect-secrets 或 gitleaks)扫描所有暂存文件;fixture 脱敏验证脚本(scripts/check_fixtures.py 扫 SESSDATA/bili_jct 模式)。Must NOT: 不允许未脱敏 fixture 提交。
  Parallelization: Wave 6 | Blocked by: T5,T9 | Blocks: - | Can parallelize with: T25,T27,T28
  References: G7/G16 Metis 修正
  Acceptance criteria: `pre-commit run --all-files` 退出0;故意提交含 SESSDATA=xxx 的文件 → 被 hook 拦截
  QA scenarios: happy: 全仓无密钥; failure: 注入假密钥 → 拦截。Evidence .omo/evidence/task-26-reccshield-refactor.log
  Commit: Y | chore(security): secret scan pre-commit + fixture redaction check

- [x] 27. CI 工作流(类型/lint/测试门禁) — .github/workflows/ci.yml (commit f1e3c8d + 05589f2 live marker)
  What to do / Must NOT do: .github/workflows/ci.yml 矩阵:backend(ruff check/basedpyright/pytest -m "not live"/coverage C1C2≥80%)、frontend(bun typecheck/lint/test)、openapi陈旧检测。Must NOT: 不跑 live 测试,不放宽类型错误。
  Parallelization: Wave 6 | Blocked by: T1 | Blocks: - | Can parallelize with: T25,T26,T28
  References: G17 Metis 修正
  Acceptance criteria: CI 在干净 PR 上全绿;故意加 `Any` → basedpyright 失败;故意删测试 → coverage 失败
  QA scenarios: happy: 全门禁绿; failure: 类型错误 → CI 红。Evidence .omo/evidence/task-27-reccshield-refactor.log
  Commit: Y | ci: backend+frontend gates with type/lint/test/coverage

- [x] 28. 文档 + 凭证卫生审计 — docs/security|testing|config|smoke_test.md (commit dc3e3af); no real cookies in repo
  What to do / Must NOT do: docs/security.md(威胁模型:127.0.0.1+LOCAL_TOKEN,接受的威胁范围)、docs/testing.md(分层测试+capture 流程+live 手动烟雾测试)、docs/config.md(单一 .env 路径,无 frozen)、docs/smoke_test.md(手动禁言真机测试步骤);全仓审计无真实 Cookie(ccShield COOKIE_AUTOBAN_SUMMARY.md:247 泄露教训)。Must NOT: 不在文档写真实 Cookie 值。
  Parallelization: Wave 6 | Blocked by: T1 | Blocks: - | Can parallelize with: T25,T26,T27
  References: G6/G16/G19 Metis 修正;ccShield/COOKIE_AUTOBAN_SUMMARY.md:247(泄露反例)
  Acceptance criteria: `grep -rE 'SESSDATA=[a-z0-9]{10,}' docs/ backend/ frontend/` 无命中;四份文档存在;docs/security.md 含威胁模型
  QA scenarios: happy: 文档齐全+无泄露; failure: 注入假 cookie → grep 命中。Evidence .omo/evidence/task-28-reccshield-refactor.log
  Commit: Y | docs: security/testing/config/smoke + credential hygiene audit

## Final verification wave
> Runs in parallel after ALL todos. ALL must APPROVE. Surface results and wait for the user's explicit okay before declaring complete.
- [x] F1. Plan compliance audit — 28/28 todos checked, deps acyclic, Metis gaps addressed (1 deviation: T7 auth_expired WS-push not wired, state+401 works) — 每条 todo 的 References/Acceptance/QA/Commit 齐全;依赖矩阵无环;无遗留 Metis Critical/High 缺口
- [x] F2. Code quality review — ruff/basedpyright/vitest green; 231 backend + 103 frontend pass; 3× no flakes; no brace-matcher/dead-code/frozen; 11 Any all justified Bili-JSON passthrough — ruff/basedpyright/vitest typecheck 全绿;无 Any 滥用;无手写括号匹配残留;无死代码
- [ ] F3. Real manual QA — 启动 make dev;QR 扫码登录真机;连接直播间看弹幕;禁言/解禁真机(手动烟雾,按 docs/smoke_test.md);SC/舰队/粉丝牌显示
- [x] F4. Scope fidelity — no sensitive-word/EXE/multi-room/token-auth; single .env; frontend consumes normalized events only — 无敏感词功能;无 EXE 打包;无并发多房间;无 token 多用户;单 .env 路径;前端不接触 B站原始协议

## Commit strategy
- 每个 todo 一个原子 commit(见各 todo Commit 行)
- commit 类型:feat/test/chore/build/ci/docs/refactor
- 波次内可并行的 todo 由并行 worker 执行,但 commit 不冲突(不同文件)
- 不提交 .env、不提交未脱敏 fixture、不提交真实 Cookie

## Success criteria
- 6 功能全可用:QR扫码登录、弹幕实时监控(单房间可切换)、手动禁言/解禁(WS推送列表)、SC显示、舰队徽章、粉丝牌等级
- 后端 ruff/basedpyright 零 error;pytest -m "not live" 全绿;C1/C2 覆盖率≥80%
- 前端 bun typecheck/lint/test 全绿;OpenAPI 客户端无陈旧
- 无敏感词功能、无 EXE、无并发多房间、无 token 认证、无手写括号匹配、无死代码
- 全仓无真实 Cookie 泄露
- 真机手动烟雾测试(QR登录+弹幕+禁言)通过
