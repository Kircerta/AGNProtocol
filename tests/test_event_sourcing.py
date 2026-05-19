from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import event_sourcing as es
from scripts.pointer_protocol import resolve_ref_path, write_text_artifact


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    event_root = tmp_path / "event_driven"
    monkeypatch.setattr(es, "EVENT_ROOT", event_root)
    monkeypatch.setattr(es, "SSOT_ROOT", event_root / "ssot")
    monkeypatch.setattr(es, "EVENTS_DIR", event_root / "ssot" / "events")
    monkeypatch.setattr(es, "CHECKPOINT_DIR", event_root / "ssot" / "checkpoints")
    monkeypatch.setattr(es, "MANIFEST_DIR", event_root / "ssot" / "manifests")
    monkeypatch.setattr(es, "PERF_DIR", event_root / "ssot" / "perf")
    monkeypatch.setattr(es, "SNAPSHOT_DIR", event_root / "ssot" / "snapshots")
    monkeypatch.setattr(es, "ACTIONS_DIR", event_root / "actions")
    monkeypatch.setattr(es, "ACTIONS_PENDING_DIR", event_root / "actions" / "pending")
    monkeypatch.setattr(es, "ACTIONS_DONE_DIR", event_root / "actions" / "done")
    monkeypatch.setattr(es, "ACTIONS_FAILED_DIR", event_root / "actions" / "failed")
    monkeypatch.setattr(es, "CONTROL_DIR", event_root / "control")
    monkeypatch.setattr(es, "CONTROL_PENDING_DIR", event_root / "control" / "pending")
    monkeypatch.setattr(es, "CONTROL_DONE_DIR", event_root / "control" / "done")
    monkeypatch.setattr(es, "CONTROL_FAILED_DIR", event_root / "control" / "failed")
    monkeypatch.setattr(es, "SCRATCH_DIR", event_root / "scratch")
    monkeypatch.setattr(es, "REPO_MAP_PATH", event_root / "ssot" / "repo_refs.json")


def test_state_transition_valid_and_invalid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    ok, reason = es.transition_state(trace_id="t-1", task_id="task-1", to_state="PLANNED", reason="plan")
    assert ok is True
    assert reason == ""

    bad_ok, bad_reason = es.transition_state(trace_id="t-1", task_id="task-1", to_state="DELIVERED", reason="skip")
    assert bad_ok is False
    assert bad_reason == "invalid_transition"

    events = es.load_events("t-1")
    assert any(e.get("event_type") == "STATE_TRANSITION" for e in events)
    assert any(e.get("event_type") == "PROTOCOL_VIOLATION" for e in events)


def test_watchdog_emits_timeout_and_recovery_action(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    stale = (datetime.now(tz=timezone.utc) - timedelta(seconds=600)).isoformat()
    es.write_checkpoint(
        "task-wd",
        {
            "task_id": "task-wd",
            "trace_id": "trace-wd",
            "state": "EXEC_RUNNING",
            "last_event_time": stale,
        },
    )
    triggered = es.watchdog_scan(timeout_sec=60)
    assert len(triggered) == 1
    events = es.load_events("trace-wd")
    assert any(e.get("event_type") == "TIMEOUT_NO_OUTPUT" for e in events)
    assert any(e.get("event_type") == "WATCHDOG_RECOVERY_PLANNED" for e in events)
    assert len(es.list_pending_actions()) == 1


def test_integrity_check_alerts_on_missing_ref(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    ref = write_text_artifact(
        task_id="task-int",
        attempt=1,
        artifact_id="victim",
        content="hello",
        media_type="text/plain",
        filename="victim.txt",
        source="test",
    ).ref
    es.append_event(trace_id="trace-int", task_id="task-int", event_type="ARTIFACT_LINKED", payload={"artifact_ref": ref})
    victim_path = Path(resolve_ref_path(ref))
    victim_path.unlink(missing_ok=True)
    check = es.integrity_check("trace-int")
    assert check["ok"] is False
    events = es.load_events("trace-int")
    assert any(e.get("event_type") == "INTEGRITY_ALERT" for e in events)


def test_resolve_main_repo_ref_falls_back_from_stale_mapping(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    stale = tmp_path / "missing-repo"
    es.register_repo_ref(repo_ref=es.build_repo_ref("main"), repo_path=str(stale))
    resolved = es.resolve_repo_ref(es.build_repo_ref("main"))
    assert resolved == es.ROOT


def test_move_action_file_is_idempotent_when_source_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    pending = es.enqueue_action(
        {
            "trace_id": "trace-idempotent",
            "task_id": "task-idempotent",
            "action_id": "act-idempotent",
            "action_type": "RETRY",
            "inputs": {},
            "refs": {},
            "budget": {},
        }
    )
    moved = es.move_action_file(pending, status="done")
    assert moved.exists()
    moved_again = es.move_action_file(pending, status="failed")
    assert moved_again.exists()
    assert moved_again.name == moved.name
