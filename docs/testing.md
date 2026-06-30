# Testing strategy

Three layers, in order of how often they run: cheap pure-logic unit tests,
contract tests backed by synthetic fixtures, and an optional live-test
suite that's skipped by default and gated behind an env flag. The
frontend mirrors the same shape with Vitest + MSW.

## Layer 1: pure-logic unit tests (always-on, TDD)

These are the tests that prove the algorithm is right. They never touch
the network and they never read `.env`. They live in
`backend/tests/test_*.py` and run on every `make test`.

Coverage priorities:

- WBI signing (`test_wbi.py`): mix-key extraction, deterministic
  reordering, the `w_rid` md5 hash. This is the gnarliest pure-logic in
  the codebase, so it's where the test density is highest.
- Cookie / config surface (`test_config.py`, `test_session.py`):
  empty-`.env` defaults, partial-`.env` behavior, the `cookies` dict
  shape.
- Banlist reconciliation (`test_banlist.py`, `test_protocol.py`):
  snapshot+delta composition, the 60-second full reconcile, ordering
  edges (empty list, single entry, full list).
- Auth (`test_auth.py`, `test_api_auth.py`): missing-token rejects,
  wrong-shape rejects, `/api/auth/bootstrap` rejects non-loopback
  origins.
- Exception envelope (`test_exceptions.py`): the unified error
  shape that goes on the wire.

Frontend equivalents: `frontend/src/**/__tests__/*.spec.ts` (Vitest).
Pure helpers, stores, and any reducer-like code go here.

## Layer 2: contract tests with synthetic fixtures (always-on)

The B站 API is the system under test's dependency, not its
implementation. We don't trust the real network in CI, but we do want
to prove our HTTP-shape assumptions hold.

The pattern, from `backend/tests/test_danmaku_contract.py` and
`test_ban_contract.py`: a fixture file under `backend/tests/fixtures/`
contains the B站 response shape as JSON; the test loads it through an
in-memory `httpx.MockTransport` and asserts on what `BilibiliClient`
returns.

Fixtures are produced by `scripts/capture_fixtures.py`:

```sh
python -m scripts.capture_fixtures --live     # from project root, with a real .env
python -m scripts.capture_fixtures --dry-run  # CI-safe default, synthetic data only
```

The live mode hits three endpoints (`/x/web-interface/nav`,
`/xlive/web-room/v1/index/getDanmuInfo`,
`/xlive/web-ucenter/v1/banned/GetSilentUserList`), then runs every
saved file through `_assert_redacted`, which scans for the patterns
`SESSDATA=[a-f0-9]{32,}` and `bili_jct=[a-f0-9]{32,}`. Any real-shaped
credential that survives redaction makes the script refuse to write the
file and raise. The dry-run mode exercises the same save+redact pipeline
on data with obvious placeholder tokens (`deadbeef*4`, `cafef00d*4`,
`a1b2c3d4*4`) so a regression in the redaction logic fails loudly on a
test that doesn't need network.

`tests/fixtures/*.json` is gitignored (see `.gitignore`) so fixture
capture is an **operator step, not a CI step**. CI runs only against
synthetic inline data; live capture is deferred to a manual gate the
operator runs on demand against a real B站 account.

Frontend: `frontend/src/test/msw/*.ts` (MSW handlers) replay the same
shapes for the TypeScript-side expectations.

## Layer 3: optional live tests (default skip)

These exist for one reason: the synthetic fixtures can drift from the
real B站 API. When B站 ships a new envelope shape, we want a way to
notice before a user notices. The live suite is the safety net.

Conventions:

- Marker on every live test:
  ```python
  @pytest.mark.live
  async def test_live_nav_returns_user_info(...): ...
  ```
- `pytest.ini` / `pyproject.toml` has `addopts` set so live markers are
  deselected by default; `make test` skips them entirely.
- Opt in with `RUN_LIVE=1`. The script reads the env var and passes
  `-m live` to pytest when set.

Scope of the live suite:

- **Read-only B站 endpoints only.** Live tests call `get_user_info`,
  `get_danmu_info`, `get_ban_list`, and similar idempotent reads.
- **No live ban.** `BilibiliClient.add_block_user` is never invoked
  from this layer. The ban code path is covered by layer-2 contract
  tests against synthetic responses, which is enough to prove the
  request shape, the CSRF token wiring, and the response parsing.
- **`SESSDATA` and `bili_jct` are required to run the suite.** A test
  that needs cookies skips with a clear "configure .env first" message
  rather than failing noisily.

If the operator wants to be extra safe, run only the read-only live
tests against a throwaway B站 account, not the production moderator
identity.

## Capture procedure (operator step)

```sh
# 1. Make sure .env has valid cookies (QR login was completed)
grep -E '^SESSDATA=.+|^BILI_JCT=.+' .env

# 2. Run capture (dry-run by default for safety)
python -m scripts.capture_fixtures --dry-run   # sanity check redaction
python -m scripts.capture_fixtures --live      # real capture
# --room <id>   overrides ROOM_ID from .env if needed
# --out-dir     defaults to tests/fixtures/; gitignored

# 3. Eyeball the saved JSON before any manual commit.
#    redaction replaces SESSDATA / bili_jct / DedeUserID* / *_token
#    values with the literal "<REDACTED>" string, drops Set-Cookie
#    headers, and clears the cookies jar.

# 4. If you actually need a captured fixture in version control
#    (rare; usually we keep them local), copy it into the test
#    contract and follow the same redaction rules.
```

## Things we don't test

- Visual appearance of the UI. Visual QA is a separate manual pass with
  screenshots, not a unit test.
- B站 WebSocket danmu protocol byte-level serialization. We depend on
  `bilibili-api` for that and rely on the library's own coverage; we
  test that our wrapper hands the right packets to it.
- Anything that requires a browser session besides the frontend unit
  tests. The smoke test (see `docs/smoke_test.md`) covers the live
  browser path.
