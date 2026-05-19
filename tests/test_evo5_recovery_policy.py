from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from agn_api.ssot_store import SSOTStore
from scripts.action_runner import run_pending
from scripts.agn_refs import build_repo_ref
from scripts.coordinator_heartbeat import run_tick
from scripts.event_sourcing import append_event, load_checkpoint, write_checkpoint
from scripts.recovery_policy import decide_recovery


def _event(event_id: str, event_type: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "payload": payload or {},
    }


def test_recovery_policy_retries_then_degrades() -> None:
    task = {"id": "task-rp", "attempt": 1, "perf_budget": {"max_time_sec": 10, "max_disk_mb": 10, "max_log_kb": 10}}
    checkpoint: dict[str, object] = {"state": "DISPATCHED_EXEC", "gate_fail_streak": 0}
    policy = {
        "retry_limits": {"TIMEOUT_NO_OUTPUT": 1, "TOOL_ERROR": 0, "PROTOCOL_VIOLATION": 0, "GATE_FAIL": 3},
        "degrade_chain": ["summary_only"],
        "escalation": {"max_total_retries": 5, "max_gate_fail_streak": 3},
    }

    d1 = decide_recovery(
        trace_id="trace-rp",
        task=task,
        checkpoint=checkpoint,
        events=[_event("evt-1", "TIMEOUT_NO_OUTPUT")],
        policy=policy,
    )
    assert d1.escalate is False
    assert len(d1.actions) == 1
    assert d1.actions[0]["action_type"] == "RETRY"

    cp2 = dict(checkpoint)
    cp2.update(d1.checkpoint_updates)
    d2 = decide_recovery(
        trace_id="trace-rp",
        task=task,
        checkpoint=cp2,
        events=[_event("evt-1", "TIMEOUT_NO_OUTPUT"), _event("evt-2", "TIMEOUT_NO_OUTPUT")],
        policy=policy,
    )
    assert d2.escalate is False
    assert len(d2.actions) == 1
    assert d2.actions[0]["action_type"] == "SUMMARIZE"


def test_escalation_policy_triggers_need_admin() -> None:
    task_id = f"task-evo5-escalate-{uuid4().hex[:8]}"
    trace_id = f"trace-evo5-escalate-{uuid4().hex[:8]}"
    store = SSOTStore(Path("ssot"))
    store.save_task(
        {
            "id": task_id,
            "source": "test",
            "request_text": "escalation test",
            "request_summary": "escalation test",
            "agn_managed": True,
            "review_requested": False,
            "status": "pending",
            "decision": None,
            "correlation_id": trace_id,
            "acceptance_criteria": [{"id": "AC-1", "text": "gate should fail"}],
            "task_kind": "protocol",
            "repo_id": "main",
            "repo_ref": build_repo_ref("main"),
            "repo_path": "",
            "work_branch": "",
            "executor_provider": "codex",
            "reviewer_provider": "gemini",
            "risk_level": "low",
            "side_effect_level": "read_only",
            "lock_state": "active",
            "runner_cmd": ["echo", "noop"],
            "attempt": 1,
            "acceptance_spec_ref": "",
        }
    )
    write_checkpoint(
        task_id,
        {
            "task_id": task_id,
            "trace_id": trace_id,
            "state": "DELIVERY_GATE",
            "paused": False,
            "gate_fail_streak": 3,
        },
    )
    append_event(trace_id=trace_id, task_id=task_id, event_type="DELIVERY_GATE_FAILED", payload={"reason": "missing refs"}, severity="warn")

    run_tick(max_tasks=2000, timeout_sec=60, task_filter=task_id, backend_name="remote_mock")

    checkpoint = load_checkpoint(task_id) or {}
    assert str(checkpoint.get("state", "")) == "NEED_ADMIN"
    assert bool(checkpoint.get("paused", False)) is True


def test_tool_error_enters_recovery_instead_of_direct_abort() -> None:
    task_id = f"task-evo5-toolerr-{uuid4().hex[:8]}"
    trace_id = f"trace-evo5-toolerr-{uuid4().hex[:8]}"
    store = SSOTStore(Path("ssot"))
    store.save_task(
        {
            "id": task_id,
            "source": "test",
            "request_text": "tool error recovery test",
            "request_summary": "tool error recovery test",
            "agn_managed": True,
            "review_requested": False,
            "status": "pending",
            "decision": None,
            "correlation_id": trace_id,
            "acceptance_criteria": [{"id": "AC-1", "text": "recover from tool error"}],
            "task_kind": "protocol",
            "repo_id": "main",
            "repo_ref": build_repo_ref("main"),
            "repo_path": "",
            "work_branch": "",
            "executor_provider": "codex",
            "reviewer_provider": "gemini",
            "risk_level": "low",
            "side_effect_level": "read_only",
            "lock_state": "active",
            "runner_cmd": ["definitely_nonexistent_binary_agn", "x"],
            "attempt": 1,
        }
    )

    for _ in range(4):
        run_tick(max_tasks=2000, timeout_sec=60, task_filter=task_id, backend_name="remote_mock")
        run_pending(max_actions=20)

    checkpoint = load_checkpoint(task_id) or {}
    state = str(checkpoint.get("state", ""))
    assert state != "ABORTED"
