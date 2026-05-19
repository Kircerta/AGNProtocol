from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import telegram_sender


def test_process_explicit_entry_sends_once_with_dedup(monkeypatch: Any) -> None:
    captured: list[dict[str, str]] = []

    def _fake_send(token: str | None, chat_id: str, text: str, dry_run: bool, timeout_sec: float) -> None:
        captured.append({"chat_id": chat_id, "text": text})

    monkeypatch.setattr(telegram_sender, "send_message", _fake_send)
    monkeypatch.setattr(telegram_sender, "append_audit", lambda **_: None)

    sent_keys: set[str] = set()
    entry = {
        "message_id": "kmsg-1",
        "chat_id": "90001",
        "task_id": "agn-explicit-1",
        "correlation_id": "corr-explicit-1",
        "kind": "progress",
        "text": "[Kirara] explicit user message",
    }

    telegram_sender.process_explicit_entry(
        entry=entry,
        sent_keys=sent_keys,
        token=None,
        dry_run=True,
        timeout_sec=1.0,
    )
    telegram_sender.process_explicit_entry(
        entry=entry,
        sent_keys=sent_keys,
        token=None,
        dry_run=True,
        timeout_sec=1.0,
    )

    assert len(captured) == 1
    assert captured[0]["chat_id"] == "90001"
    assert "explicit user message" in captured[0]["text"]


def test_explicit_mode_ignores_audit_stage_events(monkeypatch: Any) -> None:
    captured: list[str] = []

    def _fake_send(token: str | None, chat_id: str, text: str, dry_run: bool, timeout_sec: float) -> None:
        captured.append(text)

    monkeypatch.setattr(telegram_sender, "send_message", _fake_send)
    monkeypatch.setattr(telegram_sender, "append_audit", lambda **_: None)

    telegram_sender.process_event(
        event={
            "route": "/agn/reviewer",
            "action": "reviewer_processed",
            "task_id": "agn-evt-1",
            "attempt": 1,
            "chat_id": "90001",
            "correlation_id": "corr-evt-1",
        },
        sent_keys=set(),
        token=None,
        dry_run=True,
        timeout_sec=1.0,
        notify_mode="explicit",
    )

    assert captured == []


def test_explicit_mode_outbox_reading_keeps_offset(tmp_path: Path) -> None:
    outbox = tmp_path / "kirara_outbox.jsonl"
    outbox.write_text(
        "\n".join(
            [
                json.dumps({"message_id": "1", "chat_id": "90001", "text": "hello"}, ensure_ascii=True),
                json.dumps({"message_id": "2", "chat_id": "90001", "text": "world"}, ensure_ascii=True),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    next_offset, entries = telegram_sender.read_jsonl_from_offset(outbox, 0)
    assert next_offset > 0
    assert [str(item.get("message_id")) for item in entries] == ["1", "2"]

    next_offset_2, entries_2 = telegram_sender.read_jsonl_from_offset(outbox, next_offset)
    assert next_offset_2 == next_offset
    assert entries_2 == []
