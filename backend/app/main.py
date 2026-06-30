"""FastAPI application factory.

This is a minimal skeleton — no business logic yet. Health endpoint only.
"""

from __future__ import annotations

from fastapi import FastAPI


def create_app() -> FastAPI:
    """Create and return a configured FastAPI application instance."""
    app = FastAPI(
        title="reccshield backend",
        version="0.1.0",
        description="Bilibili live-room moderator tool — backend API.",
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Lightweight health probe used by liveness/readiness checks."""
        return {"status": "ok"}

    return app


app = create_app()
