"""Overload behaviour for the bounded live-message priority buffer."""

from __future__ import annotations

from app.bilibili.message_buffer import (
    MessagePriority,
    PriorityMessageBuffer,
    classify_message,
    command_name,
)


def test_command_classification_strips_suffixes() -> None:
    message: dict[str, object] = {"cmd": "DANMU_MSG:4:0:2:2:2:0"}
    assert command_name(message) == "DANMU_MSG"
    assert classify_message(message) is MessagePriority.CRITICAL
    assert classify_message({"cmd": "SEND_GIFT"}) is MessagePriority.NORMAL
    assert classify_message({"cmd": "INTERACT_WORD"}) is MessagePriority.LOW


async def test_critical_message_evicts_oldest_lower_priority() -> None:
    buffer = PriorityMessageBuffer(maxsize=3)
    buffer.put_nowait({"cmd": "INTERACT_WORD", "id": 1}, enqueued_at=1.0)
    buffer.put_nowait({"cmd": "ENTRY_EFFECT", "id": 2}, enqueued_at=2.0)
    buffer.put_nowait({"cmd": "SEND_GIFT", "id": 3}, enqueued_at=3.0)

    result = buffer.put_nowait(
        {"cmd": "DANMU_MSG", "id": 4}, enqueued_at=4.0
    )

    assert result.accepted is True
    assert result.dropped is not None
    assert result.dropped.payload["id"] == 1
    assert buffer.qsize() == 3
    assert [(await buffer.get()).payload["id"] for _ in range(3)] == [4, 3, 2]


async def test_full_critical_buffer_rejects_new_message_without_growing() -> None:
    buffer = PriorityMessageBuffer(maxsize=2)
    buffer.put_nowait({"cmd": "DANMU_MSG", "id": 1}, enqueued_at=1.0)
    buffer.put_nowait(
        {"cmd": "SUPER_CHAT_MESSAGE", "id": 2}, enqueued_at=2.0
    )

    result = buffer.put_nowait(
        {"cmd": "ROOM_BLOCK_MSG", "id": 3}, enqueued_at=3.0
    )

    assert result.accepted is False
    assert result.dropped is not None
    assert result.dropped.payload["id"] == 3
    assert buffer.qsize() == 2
    assert [(await buffer.get()).payload["id"] for _ in range(2)] == [1, 2]


def test_lower_priority_cannot_evict_critical_message() -> None:
    buffer = PriorityMessageBuffer(maxsize=1)
    buffer.put_nowait({"cmd": "DANMU_MSG"}, enqueued_at=1.0)

    result = buffer.put_nowait({"cmd": "INTERACT_WORD"}, enqueued_at=2.0)

    assert result.accepted is False
    assert result.dropped is not None
    assert result.dropped.payload["cmd"] == "INTERACT_WORD"
    assert buffer.qsize() == 1


async def test_burst_keeps_buffer_bounded_and_preserves_critical_lane() -> None:
    buffer = PriorityMessageBuffer(maxsize=100)
    for index in range(100):
        buffer.put_nowait(
            {"cmd": "INTERACT_WORD", "id": index},
            enqueued_at=float(index),
        )

    for index in range(100, 200):
        result = buffer.put_nowait(
            {"cmd": "DANMU_MSG", "id": index},
            enqueued_at=float(index),
        )
        assert result.accepted is True
        assert result.dropped is not None
        assert result.dropped.priority is MessagePriority.LOW

    for index in range(200, 10_200):
        result = buffer.put_nowait(
            {"cmd": "ENTRY_EFFECT", "id": index},
            enqueued_at=float(index),
        )
        assert result.accepted is False

    assert buffer.qsize() == 100
    assert [(await buffer.get()).payload["id"] for _ in range(100)] == list(
        range(100, 200)
    )
