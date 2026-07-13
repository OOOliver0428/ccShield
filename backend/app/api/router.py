"""Top-level ``/api/*`` router aggregator.

The single import surface (``from app.api import api_router``) is what
:mod:`app.main` mounts under ``/api`` so every protected REST route
ends up under one prefix and one middleware pass. New route modules
(``room_routes`` in T13, ``ban_routes`` in T18, etc.) are included
here without touching ``main.py``.

T13 added :data:`app.api.room_routes.router` which exposes the
``/api/rooms/*`` REST surface and the ``/api/ws/rooms/{room_id}``
WebSocket stream.

T18 added :data:`app.api.ban_routes.router` which exposes the
``/api/ban`` (POST/DELETE), ``/api/ban-list/{room_id}`` REST surface
and the ``/api/ws/rooms/{room_id}/banlist`` WebSocket stream.
"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.auth_routes import router as auth_router
from app.api.ban_routes import router as ban_router
from app.api.quick_room_routes import router as quick_room_router
from app.api.room_routes import router as room_router

api_router: APIRouter = APIRouter(prefix="/api")
"""Top-level protected API router — mounted by :mod:`app.main`."""

api_router.include_router(auth_router)
api_router.include_router(room_router)
api_router.include_router(quick_room_router)
api_router.include_router(ban_router)


__all__ = ["api_router"]



