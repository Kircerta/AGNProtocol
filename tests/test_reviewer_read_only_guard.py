from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from scripts.action_protocol import build_action
from scripts.action_runner import run_pending
from scripts.agn_refs import build_repo_ref
from scripts.event_sourcing import enqueue_action, load_events, register_repo_ref

ROOT = Path(__file__).resolve().parents[1]


def test_reviewer_write_like_command_blocked_and_evented() -> None:
    trace_id = f"trace-reviewer-guard-{uuid4().hex[:8]}"
    task_id = f"task-reviewer-guard-{uuid4().hex[:8]}"
    repo = ROOT / ".agn_workspace" / "event_driven" / "regression_repos" / f"reviewer_guard_{uuid4().hex[:6]}"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    repo_ref = build_repo_ref(f"reviewer-{uuid4().hex[:6]}")
    register_repo_ref(repo_ref=repo_ref, repo_path=str(repo))

    action = build_action(
        trace_id=trace_id,
        task_id=task_id,
        action_id=f"act-{uuid4().hex[:8]}",
        action_type="EXECUTE_CMD",
        inputs={
            "argv": ["git", "apply", "/dev/null"],
            "execution_role": "reviewer",
            "timeout_sec": 30,
            "attempt": 1,
        },
        refs={"repo_ref": repo_ref},
        budget={"max_time_sec": 30, "max_disk_mb": 32, "max_log_kb": 32},
    )
    enqueue_action(action)
    summary = run_pending(max_actions=10)

    events = load_events(trace_id)
    blocked = [e for e in events if e.get("event_type") == "ROLE_GUARD_BLOCKED"]
    finished = [e for e in events if e.get("event_type") == "ACTION_FINISHED"]

    assert summary["errors"] >= 1
    assert len(blocked) >= 1
    assert len(finished) >= 1
    payload = finished[-1].get("payload", {}) or {}
    assert int(payload.get("rc", -1) or -1) == 126
    assert str(payload.get("error_class", "")) == "ROLE_GUARD_BLOCKED"
