from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import kirara_heartbeat


def _read_outbox(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    items: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            items.append(payload)
    return items


def test_heartbeat_due_soon_alert_dedup(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(kirara_heartbeat, "append_audit", lambda **_: None)
    monkeypatch.setattr(kirara_heartbeat, "PATHS", SimpleNamespace(ssot_dir=tmp_path / "ssot"))

    tasks_path = tmp_path / "kirara_tasks.json"
    state_path = tmp_path / "kirara_heartbeat_state.json"
    outbox_path = tmp_path / "kirara_outbox.jsonl"

    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    due_at = (now + timedelta(minutes=15)).isoformat()
    tasks_path.write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "task_id": "task-due-soon-1",
                        "title": "finish parser",
                        "status": "active",
                        "due_at": due_at,
                        "chat_id": "90001",
                    }
                ]
            },
            ensure_ascii=True,
        )
        + "\n",
        encoding="utf-8",
    )

    first = kirara_heartbeat.run_tick(
        tasks_path=tasks_path,
        state_path=state_path,
        outbox_path=outbox_path,
        due_soon_minutes=30,
        stale_timeout_seconds=1800,
        cooldown_seconds=3600,
        now=now,
    )
    second = kirara_heartbeat.run_tick(
        tasks_path=tasks_path,
        state_path=state_path,
        outbox_path=outbox_path,
        due_soon_minutes=30,
        stale_timeout_seconds=1800,
        cooldown_seconds=3600,
        now=now + timedelta(minutes=5),
    )

    outbox = _read_outbox(outbox_path)
    assert first["reminders_sent"] == 1
    assert second["reminders_sent"] == 0
    assert len(outbox) == 1
    assert "Deadline approaching" in str(outbox[0].get("text", ""))


def test_heartbeat_stale_ssot_alert(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(kirara_heartbeat, "append_audit", lambda **_: None)

    ssot_dir = tmp_path / "ssot"
    ssot_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(kirara_heartbeat, "PATHS", SimpleNamespace(ssot_dir=ssot_dir))

    stale_task = ssot_dir / "agn-stale-1.json"
    stale_task.write_text('{"id":"agn-stale-1"}\n', encoding="utf-8")

    now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    old_ts = (now - timedelta(hours=2)).timestamp()
    os.utime(stale_task, (old_ts, old_ts))

    summary = kirara_heartbeat.run_tick(
        tasks_path=tmp_path / "tasks.json",
        state_path=tmp_path / "state.json",
        outbox_path=tmp_path / "outbox.jsonl",
        due_soon_minutes=30,
        stale_timeout_seconds=300,
        cooldown_seconds=3600,
        now=now,
    )
    outbox = _read_outbox(tmp_path / "outbox.jsonl")

    assert summary["stale_sent"] == 1
    assert len(outbox) == 1
    assert "heartbeat timeout" in str(outbox[0].get("text", "")).lower()
