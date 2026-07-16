from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.quick_rooms import (
    DEFAULT_QUICK_ROOMS_PATH,
    QuickRoomConfigError,
    QuickRoomRecord,
    QuickRoomStore,
)


def test_default_store_uses_shared_application_data_directory() -> None:
    from app.config import DATA_DIR

    assert DEFAULT_QUICK_ROOMS_PATH == DATA_DIR / "config" / "quick_rooms.json"


def _record(room_id: int = 1601605, *, title: str = "测试直播") -> QuickRoomRecord:
    return QuickRoomRecord(
        room_id=room_id,
        short_id=123,
        uid=42,
        uname="测试主播",
        title=title,
        live_status=1,
        added_at=datetime.now(UTC).isoformat(),
    )


@pytest.mark.asyncio
async def test_store_creates_local_json_and_loads_it(tmp_path: Path) -> None:
    path = tmp_path / "config" / "quick_rooms.json"
    store = QuickRoomStore(path)

    assert await store.list_rooms() == []
    await store.add(_record())

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["rooms"][0]["room_id"] == 1601605
    assert (await store.list_rooms())[0].uname == "测试主播"


@pytest.mark.asyncio
async def test_store_deduplicates_canonical_room_and_refreshes_metadata(
    tmp_path: Path,
) -> None:
    store = QuickRoomStore(tmp_path / "quick_rooms.json")
    first = _record(title="旧标题")
    await store.add(first)
    rooms = await store.add(_record(title="新标题"))

    assert len(rooms) == 1
    assert rooms[0].title == "新标题"
    assert rooms[0].added_at == first.added_at


@pytest.mark.asyncio
async def test_store_refuses_to_overwrite_malformed_manual_config(
    tmp_path: Path,
) -> None:
    path = tmp_path / "quick_rooms.json"
    path.write_text("{ broken", encoding="utf-8")
    store = QuickRoomStore(path)

    with pytest.raises(QuickRoomConfigError):
        await store.add(_record())
    assert path.read_text(encoding="utf-8") == "{ broken"


@pytest.fixture
def client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    from app.api import quick_room_routes, room_routes
    from app.main import create_app

    store = QuickRoomStore(tmp_path / "quick_rooms.json")
    monkeypatch.setattr(quick_room_routes, "quick_room_store", store)

    bili = AsyncMock()
    bili.resolve_room_id = AsyncMock(
        return_value={
            "room_id": 1601605,
            "short_id": 123,
            "uid": 42,
            "uname": "测试主播",
            "title": "测试直播",
            "live_status": 1,
            "is_short_id": True,
        }
    )
    monkeypatch.setattr(room_routes, "_get_bili_client", lambda: bili)

    with TestClient(create_app()) as test_client:
        yield test_client


def _headers() -> dict[str, str]:
    return {
        "Host": "localhost",
        "Authorization": f"Bearer {settings.LOCAL_TOKEN}",
    }


def test_api_lists_empty_config(client: TestClient) -> None:
    response = client.get("/api/quick-rooms", headers=_headers())
    assert response.status_code == 200
    assert response.json() == {"rooms": []}


def test_api_verifies_short_id_and_persists_canonical_room(client: TestClient) -> None:
    response = client.post(
        "/api/quick-rooms",
        json={"room_id": 123},
        headers=_headers(),
    )
    assert response.status_code == 200
    room = response.json()["rooms"][0]
    assert room["room_id"] == 1601605
    assert room["short_id"] == 123
    assert room["uname"] == "测试主播"

    listed = client.get("/api/quick-rooms", headers=_headers()).json()["rooms"]
    assert listed == response.json()["rooms"]


def test_api_rejects_invalid_room_number(client: TestClient) -> None:
    response = client.post(
        "/api/quick-rooms",
        json={"room_id": 0},
        headers=_headers(),
    )
    assert response.status_code == 422


def test_api_has_no_delete_operation(client: TestClient) -> None:
    response = client.delete("/api/quick-rooms", headers=_headers())
    assert response.status_code == 405
    operations = client.get("/openapi.json").json()["paths"]["/api/quick-rooms"]
    assert "delete" not in operations
