# Task 2-5 Evidence — config + protocol + wbi/client + test suite

**Verdict**: T2/T3/T4 confirmed (integration verifier); T5 satisfied by T3/T4 TDD (impl+test merged).
**Date**: 2026-06-30

## Commits
- T2: `5a4d6e1 feat(config): single-path pydantic-settings, startup local token, cors origins`
- T3: `4b55659 feat(protocol): bili ws frame parser, drop brace-matcher`
- T4: `645e53f feat(bilibili): typed http client + ported wbi, wbi only for getDanmuInfo`

## TDD red→green (each worker)
- T2: test_config.py first → ModuleNotFoundError → impl → 11/11 pass
- T3: test_protocol.py first → ModuleNotFoundError → impl → 28/28 pass
- T4: test_wbi/client/exceptions first → ModuleNotFoundError ×3 → impl → 44/44 pass

## Integration verification (independent verifier, real exit codes)
- `uv run ruff check .` → 0
- `uv run basedpyright` → 0 errors (whole backend together)
- `uv run pytest -v` → 85 passed (all tests together)
- 3 commits present; worker commits pristine (git diff HEAD~3..HEAD = 11 expected files only)

## Acceptance per todo
- T2: no sys.frozen/get_external_path/resource_path; cors_origins has :5173+:8000; LOCAL_TOKEN 32-hex via secrets.token_hex(16); single-path .env
- T3: NO handwritten brace-matcher (grep brace_depth/in_string = none); uses json.loads + recursion for nested sub-frames
- T4: WBI only in get_danmu_info (sign calls at 2 lines, both inside that method); NO duplicate running-check (single check, not ccShield:310-318 pattern); typed exceptions -101/-403/-509; get_ban_list running-check deferred to T17 (documented, is_running callback wired)
- T5: protocol 100% / wbi 90% / client 86% / exceptions 94% coverage — all ≥80%

## Integration probes
- client.py → config.py: lazy one-way import (line 92); config.py does NOT import bilibili (no circular)
- client.py ↛ protocol.py: HTTP client and WS protocol independent (grep = none)

## Known minor (non-blocking)
- `backend/.coverage` test artifact untracked — workers `git add` specific files so it won't be committed; add to .gitignore in a future hygiene pass.
- Literal `--cov=app.bilibili` without test_protocol.py selection shows 71% (test-selection quirk); with protocol tests, 90%. Per-file all ≥80%.

## Cleanup
- No QA assets left running. Mock-based tests, no network.
