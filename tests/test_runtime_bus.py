from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import runtime_bus as bus


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bus_root = tmp_path / "runtime" / "bus"
    monkeypatch.setattr(bus, "BUS_ROOT", bus_root)
    monkeypatch.setattr(bus, "MESSAGES_DIR", bus_root / "messages")
    monkeypatch.setattr(bus, "ACKS_DIR", bus_root / "acks")
    monkeypatch.setattr(bus, "TOPICS_DIR", bus_root / "topics")
    monkeypatch.setattr(bus, "DEAD_LETTER_DIR", bus_root / "dead_letter")
    monkeypatch.setattr(bus, "INDEX_PATH", bus_root / "index.jsonl")


def test_publish_and_ack_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    persisted: list[tuple[str, str]] = []
    monkeypatch.setattr(bus, "_persist_event", lambda event_type, message, **payload: persisted.append((event_type, str(message.get("related_trace", "")))))

    message = bus.publish_message(
        {
            "from": "dispatcher",
            "to": "desktop_adapter",
            "type": "dispatch.request",
            "topic": "dispatch.desktop_adapter",
            "summary": "desktop action",
            "payload_ref": "/tmp/request.json",
            "priority": "medium",
            "ack_required": True,
            "related_task": "task-1",
            "related_trace": "trace-1",
        }
    )

    state = bus.effective_message_state(message["id"])
    assert state["ack_status"] == "pending"
    ack = bus.acknowledge_message(message["id"], actor="desktop_adapter")
    assert ack["ack_status"] == "acked"
    state = bus.effective_message_state(message["id"])
    assert state["delivery_status"] == "delivered"
    assert persisted[0] == ("BUS_MESSAGE_PUBLISHED", "trace-1")
    assert persisted[1] == ("BUS_MESSAGE_ACKED", "trace-1")


def test_expire_message_moves_to_dead_letter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(bus, "_persist_event", lambda *_args, **_kwargs: None)

    message = bus.publish_message(
        {
            "id": "msg-expire",
            "ts": "2026-03-13T00:00:00+00:00",
            "from": "dispatcher",
            "to": "reviewer",
            "type": "dispatch.request",
            "topic": "dispatch.reviewer",
            "summary": "stale review",
            "payload_ref": "/tmp/review.json",
            "priority": "high",
            "ttl_sec": 1,
            "ack_required": True,
            "related_task": "task-2",
            "related_trace": "trace-2",
        }
    )

    expired = bus.expire_messages(now=datetime(2026, 3, 13, 0, 0, 5, tzinfo=timezone.utc))
    assert expired
    state = bus.effective_message_state(message["id"])
    assert state["ack_status"] == "dead_letter"
    assert (bus.DEAD_LETTER_DIR / "msg-expire.ttl_expired.json").exists()
