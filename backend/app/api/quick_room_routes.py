"""Read-only-list and append-only API for verified quick room shortcuts."""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.room_routes import resolve_room_route
from app.quick_rooms import (
    QuickRoomConfigError,
    QuickRoomRecord,
    quick_room_store,
)

router = APIRouter(prefix="/quick-rooms", tags=["quick-rooms"])


class AddQuickRoomRequest(BaseModel):
    """A real or short room id to verify and persist."""

    model_config = ConfigDict(extra="forbid")

    room_id: int = Field(gt=0, description="Real or short room id")


class QuickRoomListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rooms: list[QuickRoomRecord]


def _config_error(exc: QuickRoomConfigError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=str(exc),
    )


@router.get(
    "",
    response_model=QuickRoomListResponse,
    summary="List locally configured quick room shortcuts",
)
async def list_quick_rooms_route() -> QuickRoomListResponse:
    try:
        rooms = await quick_room_store.list_rooms()
    except QuickRoomConfigError as exc:
        raise _config_error(exc) from exc
    return QuickRoomListResponse(rooms=rooms)


@router.post(
    "",
    response_model=QuickRoomListResponse,
    summary="Verify and append a quick room shortcut",
)
async def add_quick_room_route(body: AddQuickRoomRequest) -> QuickRoomListResponse:
    # Resolve on the backend even if the UI already tested the number. This
    # prevents stale or fabricated room metadata from entering the local file.
    resolved = await resolve_room_route(body.room_id)
    record = QuickRoomRecord(
        room_id=resolved.room_id,
        short_id=resolved.short_id,
        uid=resolved.uid,
        uname=resolved.uname,
        title=resolved.title,
        live_status=resolved.live_status,
        added_at=datetime.now(UTC).isoformat(),
    )
    try:
        rooms = await quick_room_store.add(record)
    except QuickRoomConfigError as exc:
        raise _config_error(exc) from exc
    return QuickRoomListResponse(rooms=rooms)


__all__ = [
    "AddQuickRoomRequest",
    "QuickRoomListResponse",
    "add_quick_room_route",
    "list_quick_rooms_route",
    "router",
]
