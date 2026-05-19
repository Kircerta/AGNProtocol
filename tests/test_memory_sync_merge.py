from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from memory_sync import merge_memory_events


def _event(
    *,
    event_id: str,
    instance_id: str,
    ts: str,
    clock: int,
    key: str,
    payload: object,
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "instance_id": instance_id,
        "ts": ts,
        "logical_clock": clock,
        "key": key,
        "payload": payload,
        "kind": "test",
        "source": "pytest",
    }


def test_merge_memory_events_lww_selects_latest() -> None:
    merged = merge_memory_events(
        [
            _event(
                event_id="node-a:1",
                instance_id="node-a",
                ts="2026-03-01T10:00:00+00:00",
                clock=1,
                key="kirara_task:t-1",
                payload={"status": "active"},
            ),
            _event(
                event_id="node-b:5",
                instance_id="node-b",
                ts="2026-03-01T10:05:00+00:00",
                clock=5,
                key="kirara_task:t-1",
                payload={"status": "done"},
            ),
        ]
    )

    winner = merged["latest_by_key"]["kirara_task:t-1"]
    assert winner["payload"] == {"status": "done"}
    assert winner["event_id"] == "node-b:5"


def test_merge_memory_events_records_conflict_for_different_payloads() -> None:
    merged = merge_memory_events(
        [
            _event(
                event_id="node-a:3",
                instance_id="node-a",
                ts="2026-03-01T10:01:00+00:00",
                clock=3,
                key="kirara_tasks:registry",
                payload={"tasks": [{"task_id": "a"}]},
            ),
            _event(
                event_id="node-b:4",
                instance_id="node-b",
                ts="2026-03-01T10:02:00+00:00",
                clock=4,
                key="kirara_tasks:registry",
                payload={"tasks": [{"task_id": "b"}]},
            ),
        ]
    )

    assert merged["distinct_keys"] == 1
    assert len(merged["conflicts"]) == 1
    conflict = merged["conflicts"][0]
    assert conflict["key"] == "kirara_tasks:registry"
    assert conflict["resolution"] == "lww"
    assert conflict["winner_event_id"] == "node-b:4"
