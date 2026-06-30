"""Top-level ``/api/*`` router aggregator.

The single import surface (``from app.api import api_router``) is what
:mod:`app.main` mounts under ``/api`` so every protected REST route
ends up under one prefix and one middleware pass. New route modules
(``room_routes`` in T13, etc.) are included here without touching
``main.py``.

T13 will append::

    from app.api.room_routes import router as room_router
    api_router.include_router(room_router)

for now the aggregator only wires :data:`app.api.auth_routes.router`.
"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.auth_routes import router as auth_router

api_router: APIRouter = APIRouter(prefix="/api")
"""Top-level protected API router — mounted by :mod:`app.main`."""

api_router.include_router(auth_router)


__all__ = ["api_router"]
