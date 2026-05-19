from __future__ import annotations

import builtins
from pathlib import Path
from uuid import uuid4

from agn_api.ssot_store import SSOTStore
from scripts.action_runner import run_pending
from scripts.agn_refs import build_repo_ref
from scripts.coordinator_backend import RemoteMockBackend
from scripts.coordinator_heartbeat import run_tick
from scripts.event_sourcing import load_checkpoint, load_events, write_checkpoint

ROOT = Path(__file__).resolve().parents[1]


def _new_task(*, task_id: str, trace_id: str) -> dict[str, object]:
    return {
        "id": task_id,
        "source": "test",
        "request_text": "remote coordinator separation",
        "request_summary": "remote coordinator separation",
        "agn_managed": True,
        "review_requested": False,
        "decision": None,
        "status": "pending",
        "correlation_id": trace_id,
        "acceptance_criteria": [{"id": "AC-1", "text": "reach delivered"}],
        "task_kind": "protocol",
        "repo_ref": build_repo_ref("main"),
        "repo_id": "main",
        "repo_path": "",
        "work_branch": "",
        "executor_provider": "codex",
        "reviewer_provider": "gemini",
        "risk_level": "low",
        "side_effect_level": "read_only",
        "lock_state": "active",
        "runner_cmd": ["echo", "remote-separation-ok"],
        "attempt": 1,
    }


def test_remote_mock_backend_propose_actions_without_file_io(monkeypatch) -> None:
    backend = RemoteMockBackend()
    snapshot = {
        "trace_id": "trace-x",
        "task_id": "task-x",
        "state": "PLANNED",
        "paused": False,
        "task_spec": {
            "attempt": 1,
            "runner_cmd": ["echo", "x"],
            "review_requested": False,
            "needs_context_read": False,
            "repo_ref": build_repo_ref("main"),
            "request_text_ref": "",
            "task_spec_ref": "",
        },
        "pending_actions": [],
        "checkpoint": {},
        "perf_budget": {"max_time_sec": 30, "max_disk_mb": 10, "max_log_kb": 10},
    }

    original_open = builtins.open

    def _blocked_open(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("RemoteMockBackend must not perform file IO")

    monkeypatch.setattr(builtins, "open", _blocked_open)
    try:
        actions = backend.propose_actions(
            snapshot=snapshot,
            recent_event_digests=[],
            control_commands=[],
            ref_index=[],
        )
    finally:
        monkeypatch.setattr(builtins, "open", original_open)

    assert len(actions) == 1
    assert actions[0]["action_type"] == "EXECUTE_CMD"


def test_remote_mock_backend_drives_task_to_delivered() -> None:
    task_id = f"test-remote-mock-{uuid4().hex[:8]}"
    trace_id = f"trace-remote-mock-{uuid4().hex[:8]}"
    store = SSOTStore(ROOT / "ssot")
    store.save_task(_new_task(task_id=task_id, trace_id=trace_id))

    final_state = ""
    for _ in range(10):
        run_tick(max_tasks=2000, timeout_sec=60, task_filter=task_id, backend_name="remote_mock")
        run_pending(max_actions=20)
        checkpoint = load_checkpoint(task_id) or {}
        final_state = str(checkpoint.get("state", ""))
        if final_state == "DELIVERED":
            break

    assert final_state == "DELIVERED"
    events = load_events(trace_id)
    assert any(event.get("event_type") == "STATE_SNAPSHOT_CREATED" for event in events)
    assert not any(event.get("event_type") == "PROTOCOL_VIOLATION" for event in events)


def test_invalid_checkpoint_state_is_recovered() -> None:
    task_id = f"test-invalid-state-{uuid4().hex[:8]}"
    trace_id = f"trace-invalid-state-{uuid4().hex[:8]}"
    store = SSOTStore(ROOT / "ssot")
    store.save_task(_new_task(task_id=task_id, trace_id=trace_id))
    write_checkpoint(task_id, {"task_id": task_id, "trace_id": trace_id, "state": "HACKED", "paused": False})

    run_tick(max_tasks=2000, timeout_sec=60, task_filter=task_id, backend_name="remote_mock")
    checkpoint = load_checkpoint(task_id) or {}
    assert str(checkpoint.get("state", "")) != "HACKED"
    events = load_events(trace_id)
    assert any(
        event.get("event_type") == "PROTOCOL_VIOLATION"
        and "invalid_checkpoint_state" in str((event.get("payload", {}) or {}).get("reason", ""))
        for event in events
    )


def test_same_correlation_id_does_not_contaminate_trace() -> None:
    trace_id = f"trace-shared-{uuid4().hex[:8]}"
    task_a = f"task-shared-a-{uuid4().hex[:6]}"
    task_b = f"task-shared-b-{uuid4().hex[:6]}"
    store = SSOTStore(ROOT / "ssot")
    store.save_task(_new_task(task_id=task_a, trace_id=trace_id))
    store.save_task(_new_task(task_id=task_b, trace_id=trace_id))

    summaries = [
        run_tick(max_tasks=20, timeout_sec=60, task_filter=task_a, backend_name="remote_mock"),
        run_tick(max_tasks=20, timeout_sec=60, task_filter=task_b, backend_name="remote_mock"),
    ]
    trace_by_task = {
        str(item.get("task_id", "")): str(item.get("trace_id", ""))
        for summary in summaries
        for item in summary.get("summaries", [])
        if isinstance(item, dict)
    }
    assert task_a in trace_by_task and task_b in trace_by_task
    assert trace_by_task[task_a] != trace_by_task[task_b]
