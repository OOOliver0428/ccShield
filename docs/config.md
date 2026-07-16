# Configuration

This tool has exactly one source of configuration: the project-root
`.env` file. There is no PyInstaller bundle, no per-user config dir, no
environment-specific overlay, no second path. If you need a different
value, you edit `.env` and restart the process.

## Path

The `.env` file lives at the repo root: `<repo>/.env`.

Concretely, with `backend/app/config.py` at
`<repo>/backend/app/config.py`, the file is resolved as:

```python
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"
```

`pydantic-settings` loads it via `SettingsConfigDict(env_file=...)`.
Missing files fall through to declared defaults, so a fresh clone
starts up cleanly with empty cookies and prompts the QR login.

## Keys

| Key | Required | Default | Purpose |
| --- | --- | --- | --- |
| `SESSDATA` | yes (for login) | `""` | B站 session cookie, QR-acquired. |
| `BILI_JCT` | yes (for login) | `""` | B站 CSRF token, paired with SESSDATA. |
| `BUVID3` | no | `None` | Optional device cookie for some endpoints. |
| `ROOM_ID` | no | `None` | Default target room id at startup. |
| `HOST` | no | `127.0.0.1` | Bind address. See security note below. |
| `PORT` | no | `8000` | Bind port for the FastAPI app. |

All fields are read case-sensitively (`case_sensitive=True` in
`SettingsConfigDict`). Keys must be spelled exactly as above.

Anything else in `.env` is ignored (`extra="ignore"`).

## What is NOT supported

- **PyInstaller / frozen dual-path.** The original ccShield config
  walked four candidate `.env` paths so a bundled executable could find
  one. reccshield ships as a runnable Python process (`uv run`), so the
  candidate-path logic is gone. Adding it back later (if/when there's a
  real reason to) is the only deprecation I expect on this file. Don't
  add a `sys.frozen` branch preemptively.
- **Per-user config in `~/.config/reccshield`.** Not needed for single
  user. Same answer as above: don't add it preemptively.
- **`.env.local`, `.env.production`, Next-style overlays.** One file,
  one environment. Keep it boring.

## `.env.example`

The template is committed: `<repo>/.env.example`. All values are
empty or set to documented defaults:

```sh
# Bilibili authentication cookies
SESSDATA=
BILI_JCT=
BUVID3=

# Target live room
ROOM_ID=

# Server bind
HOST=127.0.0.1
PORT=8000
```

Copy it to `.env`, fill in real values. Don't commit `.env`.

`.gitignore` does the obvious thing:

```gitignore
.env
.env.*
!.env.example
```

## User-local shortcut state

Quick-room shortcuts are separate user state, not application settings. They are
stored in `<repo>/config/quick_rooms.json`; the file is created on first use and
must stay on the user's machine. Only the empty/example structure at
`config/quick_rooms.example.json` is committed.

Both `.env` and `config/quick_rooms.json` are ignored by Git. The CI secret gate
also rejects these exact paths if somebody bypasses `.gitignore` with a forced
add, so personal cookies and shortcut-room choices cannot be included in a
normal release commit.

## Security note on `HOST`

The default `HOST=127.0.0.1` is part of the threat model in
`docs/security.md`, not just a convenience. Changing it to `0.0.0.0`
exposes the moderator UI (and the `LOCAL_TOKEN` bootstrap endpoint) to
the LAN. Only override `HOST` to a non-loopback alias if you
understand and accept:

1. Any host on the network can reach the API.
2. The `/api/auth/bootstrap` endpoint's loopback-only check no longer
   applies; the `LOCAL_TOKEN` is readable by anyone who can issue a
   `GET /api/auth/bootstrap` from the host with the right `Host`
   header.
3. CORS widens accordingly. The current CORS list is hardcoded to
   loopback aliases; it does **not** widen when you change `HOST`.

If you need a non-loopback bind, the CORS list and the bootstrap-host
check both need to change in lockstep, and that change needs the same
threat-model conversation that this file is deferring.

## Editing at runtime

`pydantic-settings` reads `.env` once at construction. `Settings()` is a
module-level singleton, so it's constructed once on first import.
Editing `.env` while uvicorn is running does not pick up new values;
restart the process.

`LOCAL_TOKEN` is generated lazily on first property access and cached at
module scope. Restarting the process gives a fresh token; existing tabs
lose their bootstrap token and must re-bootstrap.
