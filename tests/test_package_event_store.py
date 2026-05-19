from __future__ import annotations

from pathlib import Path

import pytest

from agn.dispatch import event_store as es


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


def test_package_event_store_exposes_metadata() -> None:
    assert es.PACKAGE_PATH == "agn.dispatch.event_store"
    assert es.LEGACY_SCRIPT_SHIM == "scripts/event_sourcing.py"


def test_package_event_store_transition_state_writes_checkpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path)
    ok, reason = es.transition_state(trace_id="trace-pkg-es", task_id="task-pkg-es", to_state="PLANNED", reason="plan")

    assert ok is True
    assert reason == ""
    checkpoint = es.load_checkpoint("task-pkg-es")
    assert checkpoint is not None
    assert checkpoint["state"] == "PLANNED"
