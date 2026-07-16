# Security model

This is a self-use B站 moderator tool. It runs as a local FastAPI service on the
user's own machine, binds the loopback interface only, and trusts no network
attacker. It does, however, hold a real B站 session cookie (SESSDATA,
bili_jct, buvid3) in plain text on disk so the QR login does not need to
repeat every restart.

The model below names what we defend against, what we explicitly accept,
and what is out of scope.

## Bind surface

The server defaults to **`127.0.0.1`** in `backend/app/config.py`:

```python
HOST: str = "127.0.0.1"
PORT: int = 8000
```

The source launcher starts uvicorn on that address. The Windows Release
launcher forces `127.0.0.1` and selects an available local port starting at
8000. Source users can override `HOST`, but setting it to a non-loopback
address exposes the management API to the network and is unsupported.

CORS is locked to the loopback development origins for Vite (`5173`) and the
default FastAPI port (`8000`):

```
http://localhost:5173
http://127.0.0.1:5173
http://localhost:8000
http://127.0.0.1:8000
```

A browser cross-origin request from any non-listed origin is rejected by the
CORS preflight before it hits the handlers. The Release frontend is served by
FastAPI on the same dynamically selected origin, so it does not require an
additional CORS origin.

## LOCAL_TOKEN

Every HTTP and WebSocket request to the local API must carry a
`LOCAL_TOKEN` header (or query parameter for `/api/ws/*`). The token is
**generated once per process** in `app/config.py`:

```python
@property
def LOCAL_TOKEN(self) -> str:
    global _LOCAL_TOKEN_CACHE
    if _LOCAL_TOKEN_CACHE is None:
        _LOCAL_TOKEN_CACHE = secrets.token_hex(16)
    return _LOCAL_TOKEN_CACHE
```

`secrets.token_hex(16)` gives 32 lowercase hex chars (128 bits of
entropy). It is cached at module scope and stays the same for the lifetime
of the Python process, so the value is stable across requests and across
`Settings()` instances within one run.

The Vite frontend stores the token in `localStorage` on first load via a
bootstrap fetch to `/api/auth/bootstrap`, which is **only reachable from
`127.0.0.1` and `localhost`**: it rejects all other origins and
non-localhost host headers. This means a malicious page on the public
internet cannot read the token out of your session, because it cannot
make a request to `127.0.0.1` from a different origin (the browser
forbids it).

The token serves three purposes at once:

1. **CSRF defense**: cross-origin attackers can't read the response even
   if they could post to it, and they can't get the token in the first
   place (see below).
2. **DNS-rebinding defense**: even if a captive portal or local DNS
   resolver rebinds an attacker-controlled hostname to `127.0.0.1`, the
   browser still can't attach `LOCAL_TOKEN` to a request it didn't
   initiate from the bootstrap origin. The bootstrap endpoint only
   returns the token to requests whose `Host` header is one of the
   loopback aliases.
3. **Session isolation per process**: every restart gets a fresh token,
   so a stale tab from a previous session won't authenticate against a
   fresh process.

## Credentials

The **only credential this tool holds is the B站 cookie**:
`SESSDATA`, `bili_jct`, and optional `buvid3`. It is acquired through the
QR login flow (`getQrCode` → `pollQr` → `applyCookieHeaders`) and written as
plain text. Source mode uses `<repo-root>/.env`; Windows Release mode uses
`%LOCALAPPDATA%\ccShield\.env`.

Plain text on disk is the **accepted risk**: this is a single-user local
tool, the cookie is rotated by re-scanning the QR if it leaks, and
keeping it where `pydantic-settings` can read it on startup is the
simplest thing that works.

The cookie is never written to:

- `docs/` (this directory): any match in a markdown file is a leak.
- `tests/fixtures/`: `scripts/capture_fixtures.py` runs an `_assert_redacted`
  self-test on every saved fixture and refuses to write one that still
  contains a `SESSDATA=[hex32+]` or `bili_jct=[hex32+]` pattern.
- Application logs: `loguru` is configured with structured fields, not
  free-form string interpolation, so a request body that happens to
  contain a cookie header will not be echoed verbatim.

The credential hygiene audit is run as:

```sh
grep -rE 'SESSDATA=[a-f0-9]{10,}|bili_jct=[a-f0-9]{20,}' \
  . --exclude-dir=.git --exclude-dir=node_modules \
  --exclude-dir=.venv --exclude-dir=dist
```

Anything the regex hits is by definition a credential-shaped string. A
real hit must be either redacted or removed before commit.

Note: there is one pre-existing **known clean exception**. The capture
script under `backend/scripts/capture_fixtures.py` (and its compiled
`.pyc` cache) intentionally contains placeholder hex strings shaped to
look like real SESSDATA and bili_jct values, so that the script's
`_assert_redacted` self-test can prove the redaction pipeline actually
scrubs something. They are obviously fake (a recognizable repeating
`deadbeef` / `cafef00d` pattern, not random entropy). When you see the
audit flag these, that's by design. Verify visually, then ignore.

To avoid the audit hitting this document, the literal placeholder
strings are not reproduced here. Read
`scripts/capture_fixtures.py` directly if you want to see them.

## Threats in scope

1. **Local browser-based CSRF / DNS-rebinding.** A web page on the
   public internet trying to read or POST to `127.0.0.1:8000`. Mitigated
   by `LOCAL_TOKEN` + bootstrap-origin check + CORS allowlist.

2. **Browser extension or other local page stealing the token.** Out of
   practical mitigation. An extension with `host_permissions` set to the
   loopback origin can read `localStorage` on the same origin. The
   defense is to not install untrusted extensions in the browser profile
   used for this tool.

3. **Accidental credential commit.** Mitigated by `.gitignore` on
   `.env` (with `.env.example` kept tracked), the regex audit above, and
   the `capture_fixtures` redaction self-test.

4. **Replay of a captured (but not-yet-revoked) SESSDATA.** This is the
   one accepted-as-design risk. The cookie has whatever lifetime B站
   gives it; rotating requires re-scanning the QR. Don't share the `.env`.

## Threats explicitly out of scope

1. **Remote network attackers.** The server doesn't accept connections
   from anything other than the loopback. There's no firewall to write
   because there's no listening socket on the LAN.

2. **A malicious web page that has not somehow obtained the
   `LOCAL_TOKEN`.** The browser same-origin policy plus the
   `Host`-header check on `/api/auth/bootstrap` already block this.

3. **Another local process on the same machine reading the local `.env`.** This
   is the one we don't defend against. A hostile local process with file
   read access can read `.env`, read memory of the running uvicorn
   process, sniff loopback traffic, or do anything else a local user
   could. If a local user is compromised, this tool has no extra defense
   to offer. Out of scope.

4. **Wider supply-chain threats on `uv`/`pypi` packages.** Same answer
   as above: this is "is my laptop compromised?" territory, not
   something the tool itself can defend.
