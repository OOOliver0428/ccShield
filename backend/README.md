# reccshield backend

FastAPI service for the reccshield Bilibili live-room moderator tool.

## Quick start

```bash
uv sync --extra dev
uv run uvicorn app.main:app --reload --port 8000
```

## Tests

```bash
uv run pytest
```

## Lint / types

```bash
uv run ruff check .
uv run basedpyright
```