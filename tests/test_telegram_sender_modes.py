from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import telegram_sender


def _capture_send(monkeypatch: Any) -> list[dict[str, str]]:
    captured: list[dict[str, str]] = []

    def _fake_send(token: str | None, chat_id: str, text: str, dry_run: bool, timeout_sec: float) -> None:
        captured.append({"chat_id": chat_id, "text": text})

    monkeypatch.setattr(telegram_sender, "send_message", _fake_send)
    monkeypatch.setattr(telegram_sender, "append_audit", lambda **_: None)
    return captured


def test_sender_final_mode_sends_only_final_and_lock(monkeypatch: Any) -> None:
    sent = _capture_send(monkeypatch)
    sent_keys: set[str] = set()

    events = [
        {
            "route": "/dispatch/acks",
            "action": "executor_ack_written",
            "task_id": "tg-final-1",
            "attempt": 1,
            "chat_id": "90001",
            "correlation_id": "corr-final-1",
        },
        {
            "route": "/agn/executor",
            "action": "executor_processed",
            "task_id": "tg-final-1",
            "attempt": 1,
            "chat_id": "90001",
            "correlation_id": "corr-final-1",
        },
        {
            "route": "/agn/reviewer",
            "action": "reviewer_processed",
            "task_id": "tg-final-1",
            "attempt": 1,
            "chat_id": "90001",
            "correlation_id": "corr-final-1",
        },
        {
            "route": "/agn/coordinator",
            "action": "hallucination_lock_triggered",
            "task_id": "tg-final-2",
            "chat_id": "90001",
            "correlation_id": "corr-final-2",
            "lock_reason": "qa_retry_count_threshold_reached:3",
            "qa_retry_count": 3,
        },
    ]

    for event in events:
        telegram_sender.process_event(
            event=event,
            sent_keys=sent_keys,
            token=None,
            dry_run=True,
            timeout_sec=1.0,
            notify_mode="final",
        )

    # In final mode: no ack/exec stage messages, only review final + lock alert.
    assert len(sent) == 2
    joined = "\n".join(item["text"] for item in sent)
    assert "[AGN] FINAL" in joined
    assert "[AGN] ACK" not in joined
    assert "[AGN] EXEC" not in joined
    assert "qa_retry_count=3" in joined


def test_sender_verbose_mode_sends_ack_exec_review_with_dedup(monkeypatch: Any) -> None:
    sent = _capture_send(monkeypatch)
    sent_keys: set[str] = set()

    ack_event = {
        "route": "/dispatch/acks",
        "action": "executor_ack_written",
        "task_id": "tg-verbose-1",
        "attempt": 1,
        "chat_id": "90001",
        "correlation_id": "corr-verbose-1",
    }
    exec_event = {
        "route": "/agn/executor",
        "action": "executor_processed",
        "task_id": "tg-verbose-1",
        "attempt": 1,
        "chat_id": "90001",
        "correlation_id": "corr-verbose-1",
    }
    review_event = {
        "route": "/agn/reviewer",
        "action": "reviewer_processed",
        "task_id": "tg-verbose-1",
        "attempt": 1,
        "chat_id": "90001",
        "correlation_id": "corr-verbose-1",
    }

    # Send duplicate events; dedup key should prevent re-send.
    for event in (ack_event, ack_event, exec_event, exec_event, review_event, review_event):
        telegram_sender.process_event(
            event=event,
            sent_keys=sent_keys,
            token=None,
            dry_run=True,
            timeout_sec=1.0,
            notify_mode="verbose",
        )

    assert len(sent) == 3
    messages = "\n".join(item["text"] for item in sent)
    assert "[AGN] ACK" in messages
    assert "[AGN] EXEC" in messages
    assert "[AGN] REVIEW" in messages
