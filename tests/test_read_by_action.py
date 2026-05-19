from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from scripts.action_protocol import build_action
from scripts.action_runner import run_pending
from scripts.agn_refs import build_repo_ref
from scripts.event_sourcing import enqueue_action, load_events, register_repo_ref
from scripts.pointer_protocol import write_text_artifact

ROOT = Path(__file__).resolve().parents[1]


def _new_repo() -> Path:
    repo = ROOT / ".agn_workspace" / "event_driven" / "regression_repos" / f"read_repo_{uuid4().hex[:8]}"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "README.md").write_text("line1\nline2\nline3\n", encoding="utf-8")
    return repo


def test_read_repo_file_action_emits_read_result_created() -> None:
    repo = _new_repo()
    repo_ref = build_repo_ref(f"readrepo-{uuid4().hex[:6]}")
    register_repo_ref(repo_ref=repo_ref, repo_path=str(repo))
    trace_id = f"trace-read-repo-{uuid4().hex[:8]}"
    task_id = f"task-read-repo-{uuid4().hex[:8]}"
    action = build_action(
        trace_id=trace_id,
        task_id=task_id,
        action_id=f"act-{uuid4().hex[:8]}",
        action_type="READ_REPO_FILE",
        inputs={
            "path": "README.md",
            "line_range": {"start": 1, "end": 2},
            "max_bytes": 1024,
            "need_summary": True,
            "need_excerpt": True,
        },
        refs={"repo_ref": repo_ref},
        budget={"max_time_sec": 30, "max_disk_mb": 32, "max_log_kb": 64},
    )
    enqueue_action(action)
    summary = run_pending(max_actions=10)
    assert summary["errors"] == 0

    events = load_events(trace_id)
    read_events = [e for e in events if e.get("event_type") == "READ_RESULT_CREATED"]
    assert len(read_events) == 1
    payload = read_events[0].get("payload", {})
    assert payload.get("read_type") == "READ_REPO_FILE"
    assert payload.get("summary_ref", {}).get("ref", "").startswith("agn://")
    assert payload.get("excerpt_ref", {}).get("ref", "").startswith("agn://")


def test_read_repo_file_rejects_outside_repo_path() -> None:
    repo = _new_repo()
    repo_ref = build_repo_ref(f"readreject-{uuid4().hex[:6]}")
    register_repo_ref(repo_ref=repo_ref, repo_path=str(repo))
    trace_id = f"trace-read-reject-{uuid4().hex[:8]}"
    task_id = f"task-read-reject-{uuid4().hex[:8]}"
    action = build_action(
        trace_id=trace_id,
        task_id=task_id,
        action_id=f"act-{uuid4().hex[:8]}",
        action_type="READ_REPO_FILE",
        inputs={"path": "../outside.txt", "max_bytes": 1024},
        refs={"repo_ref": repo_ref},
        budget={"max_time_sec": 30, "max_disk_mb": 32, "max_log_kb": 64},
    )
    enqueue_action(action)
    summary = run_pending(max_actions=10)
    assert summary["errors"] >= 1

    events = load_events(trace_id)
    rejected = [e for e in events if e.get("event_type") == "READ_REJECTED"]
    assert len(rejected) >= 1
    assert any("path_forbidden" in json.dumps(e.get("payload", {}), ensure_ascii=True) for e in rejected)


def test_read_ref_respects_budget_cap() -> None:
    trace_id = f"trace-read-ref-budget-{uuid4().hex[:8]}"
    task_id = f"task-read-ref-budget-{uuid4().hex[:8]}"
    artifact = write_text_artifact(
        task_id=task_id,
        attempt=1,
        artifact_id="large_ref",
        content="Z" * 16000,
        media_type="text/plain",
        filename="large_ref.txt",
        source="test",
    )
    action = build_action(
        trace_id=trace_id,
        task_id=task_id,
        action_id=f"act-{uuid4().hex[:8]}",
        action_type="READ_REF",
        inputs={"ref": artifact.ref, "max_bytes": 10_000_000, "need_summary": True, "need_excerpt": True},
        refs={},
        budget={"max_time_sec": 30, "max_disk_mb": 32, "max_log_kb": 1},
    )
    enqueue_action(action)
    summary = run_pending(max_actions=10)
    assert summary["errors"] == 0

    events = load_events(trace_id)
    read_events = [e for e in events if e.get("event_type") == "READ_RESULT_CREATED"]
    assert read_events
    payload = read_events[-1].get("payload", {}) or {}
    assert int(payload.get("max_bytes", 0) or 0) <= 1024
    assert bool(payload.get("truncated", False)) is True
