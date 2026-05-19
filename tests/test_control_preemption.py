from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from agn_api.ssot_store import SSOTStore
from scripts.action_runner import run_pending
from scripts.coordinator_heartbeat import _fanout_broadcast_controls, run_tick
from scripts.event_sourcing import enqueue_control_command, list_pending_actions, load_checkpoint, load_events

ROOT = Path(__file__).resolve().parents[1]


def test_control_preemption_pause_modify_resume_without_duplicate_exec() -> None:
    task_id = f"task-control-{uuid4().hex[:8]}"
    trace_id = f"trace-control-{uuid4().hex[:8]}"
    store = SSOTStore(ROOT / "ssot")
    store.save_task(
        {
            "id": task_id,
            "source": "test",
            "request_text": "initial",
            "request_summary": "initial",
            "agn_managed": True,
            "review_requested": False,
            "decision": None,
            "status": "pending",
            "correlation_id": trace_id,
            "acceptance_criteria": [{"id": "AC-1", "text": "initial criterion"}],
            "task_kind": "protocol",
            "repo_path": "",
            "work_branch": "",
            "executor_provider": "codex",
            "reviewer_provider": "gemini",
            "risk_level": "low",
            "side_effect_level": "read_only",
            "lock_state": "active",
            "runner_cmd": ["echo", "control-ok"],
            "attempt": 1,
        }
    )

    enqueue_control_command(
        {
            "control_id": f"ctl-pause-{uuid4().hex[:6]}",
            "control_type": "PAUSE",
            "task_id": task_id,
            "payload": {},
        }
    )
    run_tick(max_tasks=2000, timeout_sec=60, task_filter=task_id, backend_name="remote_mock")

    enqueue_control_command(
        {
            "control_id": f"ctl-modify-{uuid4().hex[:6]}",
            "control_type": "MODIFY",
            "task_id": task_id,
            "payload": {
                "request_text": "modified request " + ("X" * 5000),
                "request_summary": "modified summary",
                "acceptance_criteria": [{"id": "AC-2", "text": "modified criterion"}],
            },
        }
    )
    enqueue_control_command(
        {
            "control_id": f"ctl-resume-{uuid4().hex[:6]}",
            "control_type": "RESUME",
            "task_id": task_id,
            "payload": {},
        }
    )

    final_state = ""
    for _ in range(12):
        run_tick(max_tasks=2000, timeout_sec=60, task_filter=task_id, backend_name="remote_mock")
        run_pending(max_actions=20)
        checkpoint = load_checkpoint(task_id) or {}
        final_state = str(checkpoint.get("state", ""))
        if final_state == "DELIVERED":
            break

    assert final_state == "DELIVERED"

    task_after = store.get_task(task_id) or {}
    assert str(task_after.get("task_spec_ref", "")).startswith("agn://")

    events = load_events(trace_id)
    applied_types = [
        str((e.get("payload", {}) or {}).get("control_type", ""))
        for e in events
        if e.get("event_type") == "CONTROL_APPLIED"
    ]
    assert "PAUSE" in applied_types
    assert "MODIFY" in applied_types
    assert "RESUME" in applied_types

    exec_started = [
        e
        for e in events
        if e.get("event_type") == "ACTION_STARTED"
        and str((e.get("payload", {}) or {}).get("action_type", "")) == "EXECUTE_CMD"
    ]
    assert len(exec_started) == 1


def test_stop_cancels_queued_actions() -> None:
    task_id = f"task-stop-{uuid4().hex[:8]}"
    trace_id = f"trace-stop-{uuid4().hex[:8]}"
    store = SSOTStore(ROOT / "ssot")
    store.save_task(
        {
            "id": task_id,
            "source": "test",
            "request_text": "stop test",
            "request_summary": "stop test",
            "agn_managed": True,
            "review_requested": False,
            "decision": None,
            "status": "pending",
            "correlation_id": trace_id,
            "acceptance_criteria": [{"id": "AC-1", "text": "stop must cancel pending"}],
            "task_kind": "protocol",
            "repo_path": "",
            "work_branch": "",
            "executor_provider": "codex",
            "reviewer_provider": "gemini",
            "risk_level": "low",
            "side_effect_level": "read_only",
            "lock_state": "active",
            "runner_cmd": ["echo", "stop-ok"],
            "attempt": 1,
        }
    )

    run_tick(max_tasks=2000, timeout_sec=60, task_filter=task_id, backend_name="remote_mock")
    pending_before = list_pending_actions(task_id=task_id, trace_id=trace_id)
    assert pending_before

    enqueue_control_command(
        {
            "control_id": f"ctl-stop-{uuid4().hex[:6]}",
            "control_type": "STOP",
            "task_id": task_id,
            "payload": {},
        }
    )
    run_tick(max_tasks=2000, timeout_sec=60, task_filter=task_id, backend_name="remote_mock")

    checkpoint = load_checkpoint(task_id) or {}
    assert str(checkpoint.get("state", "")) == "ABORTED"
    pending_after = list_pending_actions(task_id=task_id, trace_id=trace_id)
    assert not pending_after


def test_broadcast_pause_applies_to_all_tasks() -> None:
    store = SSOTStore(ROOT / "ssot")
    task_a = f"task-broadcast-a-{uuid4().hex[:8]}"
    task_b = f"task-broadcast-b-{uuid4().hex[:8]}"
    trace_a = f"trace-broadcast-a-{uuid4().hex[:8]}"
    trace_b = f"trace-broadcast-b-{uuid4().hex[:8]}"

    for task_id, trace_id in ((task_a, trace_a), (task_b, trace_b)):
        store.save_task(
            {
                "id": task_id,
                "source": "test",
                "request_text": "broadcast pause",
                "request_summary": "broadcast pause",
                "agn_managed": True,
                "review_requested": False,
                "decision": None,
                "status": "pending",
                "correlation_id": trace_id,
                "acceptance_criteria": [{"id": "AC-1", "text": "pause all"}],
                "task_kind": "protocol",
                "repo_path": "",
                "work_branch": "",
                "executor_provider": "codex",
                "reviewer_provider": "gemini",
                "risk_level": "low",
                "side_effect_level": "read_only",
                "lock_state": "active",
                "runner_cmd": ["echo", "broadcast-ok"],
                "attempt": 1,
            }
        )

    enqueue_control_command(
        {
            "control_id": f"ctl-broadcast-pause-{uuid4().hex[:6]}",
            "control_type": "PAUSE",
            "task_id": "",
            "payload": {},
        }
    )
    assert _fanout_broadcast_controls([store.get_task(task_a) or {}, store.get_task(task_b) or {}]) == 2
    run_tick(max_tasks=20, timeout_sec=60, task_filter=task_a, backend_name="remote_mock")
    run_tick(max_tasks=20, timeout_sec=60, task_filter=task_b, backend_name="remote_mock")

    cp_a = load_checkpoint(task_a) or {}
    cp_b = load_checkpoint(task_b) or {}
    assert bool(cp_a.get("paused", False)) is True
    assert bool(cp_b.get("paused", False)) is True


def test_modify_rejected_after_delivered() -> None:
    task_id = f"task-mod-after-delivered-{uuid4().hex[:8]}"
    trace_id = f"trace-mod-after-delivered-{uuid4().hex[:8]}"
    store = SSOTStore(ROOT / "ssot")
    store.save_task(
        {
            "id": task_id,
            "source": "test",
            "request_text": "initial text",
            "request_summary": "initial summary",
            "agn_managed": True,
            "review_requested": False,
            "decision": None,
            "status": "pending",
            "correlation_id": trace_id,
            "acceptance_criteria": [{"id": "AC-1", "text": "deliver first"}],
            "task_kind": "protocol",
            "repo_path": "",
            "work_branch": "",
            "executor_provider": "codex",
            "reviewer_provider": "gemini",
            "risk_level": "low",
            "side_effect_level": "read_only",
            "lock_state": "active",
            "runner_cmd": ["echo", "done"],
            "attempt": 1,
        }
    )

    final_state = ""
    for _ in range(12):
        run_tick(max_tasks=2000, timeout_sec=60, task_filter=task_id, backend_name="remote_mock")
        run_pending(max_actions=20)
        checkpoint = load_checkpoint(task_id) or {}
        final_state = str(checkpoint.get("state", ""))
        if final_state == "DELIVERED":
            break
    assert final_state == "DELIVERED"
    before = store.get_task(task_id) or {}
    before_summary = str(before.get("request_summary", ""))
    before_spec_ref = str(before.get("task_spec_ref", ""))

    enqueue_control_command(
        {
            "control_id": f"ctl-mod-reject-{uuid4().hex[:6]}",
            "control_type": "MODIFY",
            "task_id": task_id,
            "payload": {"request_summary": "mutated after delivered", "request_text": "mutated text"},
        }
    )
    run_tick(max_tasks=2000, timeout_sec=60, task_filter=task_id, backend_name="remote_mock")

    after = store.get_task(task_id) or {}
    assert str(after.get("request_summary", "")) == before_summary
    assert str(after.get("task_spec_ref", "")) == before_spec_ref
    events = load_events(trace_id)
    assert any(
        e.get("event_type") == "CONTROL_REJECTED"
        and "modify_not_allowed_in_terminal_state" in str((e.get("payload", {}) or {}).get("reason", ""))
        for e in events
    )
