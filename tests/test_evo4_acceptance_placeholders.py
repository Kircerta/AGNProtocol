from __future__ import annotations

import builtins
import json
from pathlib import Path
import re
import subprocess
from uuid import uuid4

import pytest

from agn_api.ssot_store import SSOTStore
from scripts.action_protocol import validate_action_payload
from scripts.action_runner import run_pending
from scripts.agn_refs import build_repo_ref
from scripts.coordinator_backend import BackendProtocolViolation, LocalBackend, RemoteMockBackend
from scripts.coordinator_heartbeat import run_tick
from scripts.event_sourcing import load_checkpoint, load_events, register_repo_ref

ROOT = Path(__file__).resolve().parents[1]


def _new_task(*, task_id: str, trace_id: str, repo_ref: str = "", repo_path: str = "", needs_context_read: bool = False) -> dict[str, object]:
    return {
        "id": task_id,
        "source": "test",
        "request_text": "evo4 acceptance",
        "request_summary": "evo4 acceptance",
        "agn_managed": True,
        "review_requested": False,
        "decision": None,
        "status": "pending",
        "correlation_id": trace_id,
        "acceptance_criteria": [{"id": "AC-1", "text": "reach delivered"}],
        "task_kind": "repo" if repo_path else "protocol",
        "repo_path": repo_path,
        "repo_id": "main",
        "repo_ref": repo_ref,
        "work_branch": "",
        "executor_provider": "codex",
        "reviewer_provider": "gemini",
        "risk_level": "low",
        "side_effect_level": "read_only",
        "lock_state": "active",
        "runner_cmd": ["echo", "evo4-ok"],
        "attempt": 1,
        "needs_context_read": bool(needs_context_read),
        "context_read_path": "README.md",
    }


def test_remote_mock_is_pure_no_local_deps(monkeypatch: pytest.MonkeyPatch) -> None:
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

    monkeypatch.setattr(LocalBackend, "propose_actions", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call LocalBackend")))
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

    bad_snapshot = {
        **snapshot,
        "task_spec": {**snapshot["task_spec"], "repo_path": "/tmp/should-not-exist"},
    }
    with pytest.raises(BackendProtocolViolation):
        backend.propose_actions(
            snapshot=bad_snapshot,
            recent_event_digests=[],
            control_commands=[],
            ref_index=[],
        )


def test_action_refs_are_ref_only_no_paths() -> None:
    backend = RemoteMockBackend()
    snapshot = {
        "trace_id": "trace-ref",
        "task_id": "task-ref",
        "state": "EXEC_DONE",
        "paused": False,
        "task_spec": {
            "attempt": 2,
            "review_requested": True,
            "repo_ref": build_repo_ref("main"),
            "request_text_ref": "agn://artifact/" + ("a" * 64),
            "task_spec_ref": "agn://artifact/" + ("b" * 64),
        },
        "pending_actions": [],
        "checkpoint": {},
        "perf_budget": {"max_time_sec": 30, "max_disk_mb": 10, "max_log_kb": 10},
    }
    actions = backend.propose_actions(
        snapshot=snapshot,
        recent_event_digests=[],
        control_commands=[],
        ref_index=[],
    )
    assert len(actions) == 1
    action = actions[0]
    vr = validate_action_payload(action)
    assert vr.valid is True
    refs = action.get("refs", {})
    assert set(refs.keys()) == {"dispatch_ref", "result_ref", "verdict_ref"}
    for value in refs.values():
        assert isinstance(value, str)
        assert value.startswith("agn://")
        assert not value.startswith("/")
        assert "/Users/" not in value
        assert ("/Vol" + "umes/") not in value
        assert "C:\\" not in value


def test_reports_no_absolute_paths() -> None:
    cmd = ["python3", "scripts/validation/run_event_driven_regression.py"]
    proc = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr

    line = proc.stdout.strip().splitlines()[-1]
    payload = json.loads(line)
    output = Path(payload["output"])
    report = json.loads(output.read_text(encoding="utf-8"))

    assert report.get("path_mode") == "relative_to_repo_root"
    assert report.get("repo_root") == "."

    absolute_pat = re.compile(r"(^/|[A-Za-z]:\\\\|/Users/|/Vol" + r"umes/)")
    for case in report.get("cases", []):
        for path in case.get("evidence_paths", []):
            assert not absolute_pat.search(str(path)), f"absolute path leaked: {path}"


def test_read_by_action_no_implicit_io() -> None:
    trace_id = f"trace-evo4-read-{uuid4().hex[:8]}"
    task_id = f"task-evo4-read-{uuid4().hex[:8]}"
    repo = ROOT / ".agn_workspace" / "event_driven" / "regression_repos" / f"evo4_read_repo_{uuid4().hex[:6]}"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "README.md").write_text("line1\nline2\n", encoding="utf-8")

    repo_ref = build_repo_ref(f"evo4_{uuid4().hex[:6]}")
    register_repo_ref(repo_ref=repo_ref, repo_path=str(repo))

    store = SSOTStore(ROOT / "ssot")
    store.save_task(
        _new_task(
            task_id=task_id,
            trace_id=trace_id,
            repo_ref=repo_ref,
            repo_path=str(repo),
            needs_context_read=True,
        )
    )

    run_tick(max_tasks=2000, timeout_sec=60, task_filter=task_id, backend_name="remote_mock")
    events = load_events(trace_id)
    planned = [e for e in events if e.get("event_type") == "ACTION_PLANNED"]
    assert planned
    first_type = str((planned[0].get("payload", {}) or {}).get("action_type", ""))
    assert first_type in {"READ_REPO_FILE", "READ_REF"}

    cp = load_checkpoint(task_id) or {}
    assert bool(cp.get("context_loaded", False)) is False

    summary = run_pending(max_actions=20)
    assert summary["errors"] == 0

    run_tick(max_tasks=2000, timeout_sec=60, task_filter=task_id, backend_name="remote_mock")
    events = load_events(trace_id)

    read_idx = -1
    exec_idx = -1
    for idx, event in enumerate(events):
        if str(event.get("event_type", "")) == "READ_RESULT_CREATED" and read_idx < 0:
            read_idx = idx
        if str(event.get("event_type", "")) == "ACTION_PLANNED":
            payload = event.get("payload", {}) or {}
            if str(payload.get("action_type", "")) == "EXECUTE_CMD" and exec_idx < 0:
                exec_idx = idx
    assert read_idx >= 0
    assert exec_idx > read_idx
