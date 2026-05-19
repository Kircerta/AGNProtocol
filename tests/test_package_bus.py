from __future__ import annotations

from pathlib import Path

import pytest

from agn.dispatch import bus


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bus_root = tmp_path / "runtime" / "bus"
    monkeypatch.setattr(bus, "BUS_ROOT", bus_root)
    monkeypatch.setattr(bus, "MESSAGES_DIR", bus_root / "messages")
    monkeypatch.setattr(bus, "ACKS_DIR", bus_root / "acks")
    monkeypatch.setattr(bus, "TOPICS_DIR", bus_root / "topics")
    monkeypatch.setattr(bus, "DEAD_LETTER_DIR", bus_root / "dead_letter")
    monkeypatch.setattr(bus, "INDEX_PATH", bus_root / "index.jsonl")


def test_package_bus_exposes_metadata() -> None:
    assert bus.PACKAGE_PATH == "agn.dispatch.bus"
    assert bus.LEGACY_SCRIPT_SHIM == "scripts/runtime_bus.py"


def test_package_bus_publish_and_ack(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    persisted: list[str] = []
    monkeypatch.setattr(bus, "_persist_event", lambda event_type, _message, **_payload: persisted.append(event_type))

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
            "related_task": "task-bus",
            "related_trace": "trace-bus",
        }
    )
    ack = bus.acknowledge_message(message["id"], actor="desktop_adapter")

    assert ack["ack_status"] == "acked"
    assert persisted == ["BUS_MESSAGE_PUBLISHED", "BUS_MESSAGE_ACKED"]
