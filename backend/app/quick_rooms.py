"""Local JSON persistence for user-defined quick room shortcuts.

The file is intentionally user-editable and has no delete operation in the
application.  The first version only appends or refreshes canonical room
records; removing a shortcut requires editing ``config/quick_rooms.json``
while ccShield is stopped.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import DATA_DIR

DEFAULT_QUICK_ROOMS_PATH = DATA_DIR / "config" / "quick_rooms.json"


class QuickRoomRecord(BaseModel):
    """A verified, canonical live-room shortcut stored on disk."""

    model_config = ConfigDict(extra="ignore")

    room_id: int = Field(gt=0)
    short_id: int = Field(default=0, ge=0)
    uid: int | None = None
    uname: str = ""
    title: str = ""
    live_status: int = 0
    added_at: str


class QuickRoomConfig(BaseModel):
    """Versioned on-disk envelope so the format can evolve safely."""

    model_config = ConfigDict(extra="ignore")

    version: int = 1
    rooms: list[QuickRoomRecord] = Field(default_factory=list)


class QuickRoomConfigError(RuntimeError):
    """The local config could not be parsed or persisted safely."""


class QuickRoomStore:
    """Read and atomically update the local quick-room configuration."""

    def __init__(self, path: Path = DEFAULT_QUICK_ROOMS_PATH) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    def _read_unlocked(self) -> QuickRoomConfig:
        if not self.path.exists():
            return QuickRoomConfig()
        try:
            raw: Any = json.loads(self.path.read_text(encoding="utf-8"))
            return QuickRoomConfig.model_validate(raw)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise QuickRoomConfigError(
                f"快捷房间配置无法读取: {self.path}"
            ) from exc

    def _write_unlocked(self, config: QuickRoomConfig) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
            payload = config.model_dump(mode="json")
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temp_path, self.path)
        except OSError as exc:
            raise QuickRoomConfigError(
                f"快捷房间配置无法保存: {self.path}"
            ) from exc

    async def list_rooms(self) -> list[QuickRoomRecord]:
        """Reload records from disk so manual edits are visible immediately."""

        async with self._lock:
            return list(self._read_unlocked().rooms)

    async def add(self, record: QuickRoomRecord) -> list[QuickRoomRecord]:
        """Append a room, or refresh its metadata without creating duplicates."""

        async with self._lock:
            config = self._read_unlocked()
            for index, existing in enumerate(config.rooms):
                if existing.room_id == record.room_id:
                    # Keep the original creation time while refreshing titles and
                    # anchor metadata obtained from the latest verification.
                    record = record.model_copy(update={"added_at": existing.added_at})
                    config.rooms[index] = record
                    break
            else:
                config.rooms.append(record)
            self._write_unlocked(config)
            return list(config.rooms)


quick_room_store = QuickRoomStore()


__all__ = [
    "DEFAULT_QUICK_ROOMS_PATH",
    "QuickRoomConfig",
    "QuickRoomConfigError",
    "QuickRoomRecord",
    "QuickRoomStore",
    "quick_room_store",
]
