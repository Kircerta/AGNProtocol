"""Tests for the cross-trace load_recent_events() function (H5 audit fix)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture()
def event_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Redirect event store directories to tmp_path for isolation."""
    import importlib
    from agn.dispatch import event_store as es

    events_dir = tmp_path / "events"
    events_dir.mkdir(parents=True)
    monkeypatch.setattr(es, "EVENTS_DIR", events_dir)
    monkeypatch.setattr(es, "EVENT_ROOT", tmp_path)
    monkeypatch.setattr(es, "SSOT_ROOT", tmp_path)
    monkeypatch.setattr(es, "CHECKPOINT_DIR", tmp_path / "checkpoints")
    monkeypatch.setattr(es, "MANIFEST_DIR", tmp_path / "manifests")
    monkeypatch.setattr(es, "PERF_DIR", tmp_path / "perf")
    monkeypatch.setattr(es, "SNAPSHOT_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(es, "ACTIONS_PENDING_DIR", tmp_path / "actions" / "pending")
    monkeypatch.setattr(es, "ACTIONS_DONE_DIR", tmp_path / "actions" / "done")
    monkeypatch.setattr(es, "ACTIONS_FAILED_DIR", tmp_path / "actions" / "failed")
    monkeypatch.setattr(es, "CONTROL_PENDING_DIR", tmp_path / "control" / "pending")
    monkeypatch.setattr(es, "CONTROL_DONE_DIR", tmp_path / "control" / "done")
    monkeypatch.setattr(es, "CONTROL_FAILED_DIR", tmp_path / "control" / "failed")
    monkeypatch.setattr(es, "SCRATCH_DIR", tmp_path / "scratch")
    return es


def test_load_recent_events_returns_cross_trace_results(event_dirs) -> None:
    es = event_dirs
    # Append events to different traces
    es.append_event(trace_id="trace-a", task_id="task-1", event_type="STATE_TRANSITION", severity="info")
    es.append_event(trace_id="trace-b", task_id="task-2", event_type="INTEGRITY_ALERT", severity="error")
    es.append_event(trace_id="trace-c", task_id="task-3", event_type="HEARTBEAT_TICK", severity="info")

    all_events = es.load_recent_events(max_age_seconds=0)
    assert len(all_events) >= 3

    # Filter by event_type
    alerts = es.load_recent_events(event_type="INTEGRITY_ALERT", max_age_seconds=0)
    assert len(alerts) == 1
    assert alerts[0]["event_type"] == "INTEGRITY_ALERT"

    # Filter by severity
    errors = es.load_recent_events(severity="error", max_age_seconds=0)
    assert len(errors) == 1
    assert errors[0]["severity"] == "error"


def test_load_recent_events_respects_age_filter(event_dirs) -> None:
    es = event_dirs
    # Append a recent event
    es.append_event(trace_id="trace-fresh", task_id="task-fresh", event_type="TEST", severity="info")

    # Recent events should be found
    results = es.load_recent_events(max_age_seconds=60)
    assert len(results) >= 1

    # Very short TTL should still find very recent events
    results = es.load_recent_events(max_age_seconds=1)
    assert len(results) >= 1


def test_load_recent_events_respects_limit(event_dirs) -> None:
    es = event_dirs
    for i in range(10):
        es.append_event(trace_id=f"trace-{i}", task_id=f"task-{i}", event_type="TEST", severity="info")

    results = es.load_recent_events(max_age_seconds=0, limit=3)
    assert len(results) == 3


def test_load_recent_events_sorted_newest_first(event_dirs) -> None:
    es = event_dirs
    es.append_event(trace_id="trace-first", task_id="task-1", event_type="A", severity="info")
    es.append_event(trace_id="trace-second", task_id="task-2", event_type="B", severity="info")

    results = es.load_recent_events(max_age_seconds=0)
    assert len(results) >= 2
    # Newest should come first
    timestamps = [r["ts"] for r in results if r.get("ts")]
    assert timestamps == sorted(timestamps, reverse=True)
