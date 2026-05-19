#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

if importlib.util.find_spec("httpx") is None and not os.getenv("AGN_EVENT_REGRESSION_DEPS_BOOTSTRAPPED"):
    uv = shutil.which("uv")
    if uv:
        os.environ["AGN_EVENT_REGRESSION_DEPS_BOOTSTRAPPED"] = "1"
        os.execvp(uv, [uv, "run", "--with", "httpx", "python3", *sys.argv])

from action_runner import run_pending
from agn_refs import build_repo_ref
from coordinator_heartbeat import run_tick
from event_sourcing import (
    SNAPSHOT_DIR,
    enqueue_control_command,
    load_checkpoint,
    load_events,
    register_repo_ref,
)
from agn_api.ssot_store import SSOTStore
from pointer_protocol import resolve_ref_path
from scripts import agent_runner as ar
from scripts.coordinator_ingest import run as coordinator_ingest_run


@dataclass
class CaseResult:
    case_id: str
    section: str
    command: str
    expected: str
    actual: str
    passed: bool
    evidence_paths: list[str]
    note: str = ""


def _rel(path: str | Path) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = (ROOT / p).resolve()
    else:
        p = p.resolve()
    try:
        return str(p.relative_to(ROOT))
    except Exception:
        return str(p)


def _events_path(trace_id: str) -> Path:
    return ROOT / ".agn_workspace" / "event_driven" / "ssot" / "events" / f"{trace_id}.jsonl"


def _checkpoint_path(task_id: str) -> Path:
    return ROOT / ".agn_workspace" / "event_driven" / "ssot" / "checkpoints" / f"{task_id}.json"


def _latest_snapshot_path(trace_id: str) -> Path:
    return SNAPSHOT_DIR / f"{trace_id}.snapshot.json"


def _new_task(
    *,
    task_id: str,
    trace_id: str,
    repo_path: str = "",
    review_requested: bool = False,
    needs_context_read: bool = False,
    context_read_path: str = "README.md",
    runner_cmd: list[str] | None = None,
) -> dict[str, Any]:
    repo_ref = build_repo_ref("main") if not repo_path else build_repo_ref(f"repo_{task_id}")
    return {
        "id": task_id,
        "source": "event_driven_regression",
        "request_text": "event driven task",
        "request_summary": "event driven task",
        "agn_managed": True,
        "review_requested": review_requested,
        "decision": None,
        "status": "pending",
        "correlation_id": trace_id,
        "acceptance_criteria": [{"id": "AC-1", "text": "runner executes action"}],
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
        "needs_context_read": bool(needs_context_read),
        "context_read_path": context_read_path,
        "runner_cmd": runner_cmd or ["echo", "ok"],
        "attempt": 1,
    }


def _run_until_state(*, task_id: str, backend: str, target: str, max_ticks: int = 12) -> tuple[str, int]:
    final_state = ""
    ticks = 0
    for i in range(max_ticks):
        ticks = i + 1
        run_tick(max_tasks=2000, timeout_sec=60, task_filter=task_id, backend_name=backend)
        run_pending(max_actions=20)
        cp = load_checkpoint(task_id) or {}
        final_state = str(cp.get("state", ""))
        if final_state == target:
            break
    return final_state, ticks


def _git_init_repo(base_dir: Path, name: str) -> Path:
    repo = base_dir / name
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "README.md").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=str(repo), text=True, capture_output=True, check=False)
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), text=True, capture_output=True, check=False)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo),
        text=True,
        capture_output=True,
        check=False,
        env={
            **__import__("os").environ,
            "GIT_AUTHOR_NAME": "agn-regression",
            "GIT_AUTHOR_EMAIL": "agn-regression@example.com",
            "GIT_COMMITTER_NAME": "agn-regression",
            "GIT_COMMITTER_EMAIL": "agn-regression@example.com",
        },
    )
    return repo


def _case_t1_remote_separation() -> CaseResult:
    trace_id = f"trace-t1-{uuid4().hex[:8]}"
    task_id = f"task-t1-{uuid4().hex[:8]}"
    store = SSOTStore(ROOT / "ssot")
    store.save_task(_new_task(task_id=task_id, trace_id=trace_id, runner_cmd=["echo", "remote-t1-ok"]))

    state, ticks = _run_until_state(task_id=task_id, backend="remote_mock", target="DELIVERED")
    events = load_events(trace_id)
    has_snapshot = any(e.get("event_type") == "STATE_SNAPSHOT_CREATED" for e in events)
    has_violation = any(e.get("event_type") == "PROTOCOL_VIOLATION" for e in events)
    has_planned = any(e.get("event_type") == "ACTION_PLANNED" for e in events)
    passed = state == "DELIVERED" and has_snapshot and has_planned and not has_violation

    return CaseResult(
        case_id="4.T1",
        section="Evo4 Remote Separation",
        command="run_tick(backend=remote_mock)+run_pending until DELIVERED",
        expected="RemoteMockBackend mode reaches terminal state without local repo dependency in coordinator path",
        actual=f"final_state={state} ticks={ticks} snapshots={has_snapshot} planned={has_planned} protocol_violation={has_violation}",
        passed=passed,
        evidence_paths=[
            _rel(_events_path(trace_id)),
            _rel(_checkpoint_path(task_id)),
            _rel(_latest_snapshot_path(trace_id)),
        ],
    )


def _case_t2_read_by_action() -> CaseResult:
    trace_id = f"trace-t2-{uuid4().hex[:8]}"
    task_id = f"task-t2-{uuid4().hex[:8]}"
    repo = _git_init_repo(ROOT / ".agn_workspace" / "event_driven" / "regression_repos", f"t2_repo_{uuid4().hex[:6]}")
    register_repo_ref(repo_ref=build_repo_ref(f"repo_{task_id}"), repo_path=str(repo))

    store = SSOTStore(ROOT / "ssot")
    store.save_task(
        _new_task(
            task_id=task_id,
            trace_id=trace_id,
            repo_path=str(repo),
            review_requested=False,
            needs_context_read=True,
            context_read_path="README.md",
            runner_cmd=["echo", "read-by-action-ok"],
        )
    )

    state, ticks = _run_until_state(task_id=task_id, backend="remote_mock", target="DELIVERED")
    events = load_events(trace_id)

    read_plan_idx = -1
    read_result_idx = -1
    exec_plan_idx = -1
    read_result: dict[str, Any] = {}
    for idx, event in enumerate(events):
        et = str(event.get("event_type", ""))
        payload = event.get("payload", {}) or {}
        action_type = str(payload.get("action_type", ""))
        if et == "ACTION_PLANNED" and action_type in {"READ_REPO_FILE", "READ_REF"} and read_plan_idx < 0:
            read_plan_idx = idx
        if et == "READ_RESULT_CREATED" and read_result_idx < 0:
            read_result_idx = idx
            if isinstance(payload, dict):
                read_result = payload
        if et == "ACTION_PLANNED" and action_type == "EXECUTE_CMD" and exec_plan_idx < 0:
            exec_plan_idx = idx

    order_ok = read_plan_idx >= 0 and read_result_idx >= 0 and exec_plan_idx >= 0 and read_plan_idx < read_result_idx < exec_plan_idx
    bounded = int(read_result.get("max_bytes", 0) or 0) <= 4096
    passed = state == "DELIVERED" and order_ok and bounded

    evidence = [_rel(_events_path(trace_id)), _rel(_checkpoint_path(task_id))]
    summary_ref = str((read_result.get("summary_ref", {}) or {}).get("ref", "")).strip()
    excerpt_ref = str((read_result.get("excerpt_ref", {}) or {}).get("ref", "")).strip()
    if summary_ref:
        evidence.append(summary_ref)
        evidence.append(_rel(resolve_ref_path(summary_ref)))
    if excerpt_ref:
        evidence.append(excerpt_ref)
        evidence.append(_rel(resolve_ref_path(excerpt_ref)))

    return CaseResult(
        case_id="4.T2",
        section="Evo4 Read-by-Action",
        command="task(needs_context_read=true) -> remote_mock emits READ_* then EXECUTE_CMD",
        expected="No implicit context reads; READ_RESULT_CREATED appears before execute planning",
        actual=(
            f"final_state={state} ticks={ticks} read_plan_idx={read_plan_idx} "
            f"read_result_idx={read_result_idx} exec_plan_idx={exec_plan_idx} max_bytes={read_result.get('max_bytes')}"
        ),
        passed=passed,
        evidence_paths=evidence,
    )


def _case_t3_control_preemption() -> CaseResult:
    trace_id = f"trace-t3-{uuid4().hex[:8]}"
    task_id = f"task-t3-{uuid4().hex[:8]}"
    store = SSOTStore(ROOT / "ssot")
    store.save_task(_new_task(task_id=task_id, trace_id=trace_id, runner_cmd=["echo", "t3-ok"]))

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
                "request_text": "updated " + ("Z" * 5000),
                "request_summary": "updated summary",
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

    state, ticks = _run_until_state(task_id=task_id, backend="remote_mock", target="DELIVERED")
    task_after = store.get_task(task_id) or {}
    events = load_events(trace_id)

    applied_types = [
        str((e.get("payload", {}) or {}).get("control_type", ""))
        for e in events
        if e.get("event_type") == "CONTROL_APPLIED"
    ]
    has_preemption = all(kind in applied_types for kind in ("PAUSE", "MODIFY", "RESUME"))

    exec_started = [
        e
        for e in events
        if e.get("event_type") == "ACTION_STARTED"
        and str((e.get("payload", {}) or {}).get("action_type", "")) == "EXECUTE_CMD"
    ]
    no_duplicate_exec = len(exec_started) == 1
    has_task_spec_ref = str(task_after.get("task_spec_ref", "")).startswith("agn://")
    passed = state == "DELIVERED" and has_preemption and no_duplicate_exec and has_task_spec_ref

    return CaseResult(
        case_id="4.T3",
        section="Evo4 Control Preemption",
        command="enqueue PAUSE -> MODIFY -> RESUME, then heartbeat+runner loop",
        expected="Control queue preempts planning; modified spec applied; no duplicate destructive exec",
        actual=(
            f"final_state={state} ticks={ticks} applied={sorted(set(applied_types))} "
            f"exec_started={len(exec_started)} has_task_spec_ref={has_task_spec_ref}"
        ),
        passed=passed,
        evidence_paths=[
            _rel(_events_path(trace_id)),
            _rel(_checkpoint_path(task_id)),
            _rel(ROOT / "ssot" / f"{task_id}.json"),
        ],
    )


def _case_t4_large_text_invisibility() -> CaseResult:
    task_id = f"task-t4-{uuid4().hex[:8]}"
    trace_id = f"trace-t4-{uuid4().hex[:8]}"
    huge = "L" * 120000

    ingest = coordinator_ingest_run(
        task_id=task_id,
        request_text=huge,
        source="event_driven_regression",
        correlation_id=trace_id,
        criteria_json=None,
        criterion_items=["AC-1:large text must be ref-only"],
        task_kind="protocol",
        repo_path="",
        work_branch="",
        executor_provider="codex",
        reviewer_provider="gemini",
        chat_id="",
        message_id="",
        risk_level="low",
        side_effect_level="read_only",
        attempt=None,
    )

    dispatch_path = ROOT / "dispatch" / f"{task_id}.json"
    dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
    inline_len = len(str(dispatch.get("request_text", "")))
    has_ref = bool(str(dispatch.get("request_text_ref", "")).strip())

    run_tick(max_tasks=2000, timeout_sec=60, task_filter=task_id, backend_name="remote_mock")
    snapshot_path = _latest_snapshot_path(trace_id)
    snapshot_text = snapshot_path.read_text(encoding="utf-8") if snapshot_path.exists() else ""
    snapshot_ok = snapshot_path.exists() and len(snapshot_text.encode("utf-8")) < 20000 and ("L" * 2000 not in snapshot_text)

    exec_prompt = ar._compose_codex_prompt(
        request_text=huge,
        request_summary="summary",
        request_text_ref=str(dispatch.get("request_text_ref", "")),
        acceptance_criteria=[{"id": "AC-1", "text": "T" * 40000}],
    )
    exec_compact, exec_degraded = ar._apply_executor_prompt_budget(
        prompt=exec_prompt,
        request_summary="summary",
        request_text_ref=str(dispatch.get("request_text_ref", "")),
        acceptance_criteria=[{"id": "AC-1", "text": "T" * 40000}],
    )
    reviewer_compact, reviewer_degraded = ar._apply_reviewer_prompt_budget(
        prompt="R" * 50000,
        compact_payload={"dispatch": {"acceptance_criteria": [{"id": "AC-1", "text": "ok"}]}, "result": {"artifact_refs": []}},
        context_ref="agn://artifact/" + ("a" * 64),
    )
    formatted_cmd = ar._format_cmd(["codex", "exec", "--cd", "/tmp/repo", exec_prompt])

    prompt_ok = exec_degraded and reviewer_degraded and len(exec_compact) <= ar._EXECUTOR_PROMPT_MAX_CHARS and len(reviewer_compact) <= ar._REVIEWER_PROMPT_MAX_CHARS
    log_ok = "sha256=" in formatted_cmd and len(formatted_cmd.encode("utf-8")) < 4096
    passed = bool(ingest.get("ok")) and inline_len == 0 and has_ref and snapshot_ok and prompt_ok and log_ok

    return CaseResult(
        case_id="4.T4",
        section="Evo4 Large Payload Invisibility",
        command="120k request -> dispatch + heartbeat(snapshot) + prompt budget checks",
        expected="dispatch/prompt/log/snapshot remain ref-first and bounded",
        actual=(
            f"ingest_ok={ingest.get('ok')} dispatch_inline_len={inline_len} has_ref={has_ref} "
            f"snapshot_ok={snapshot_ok} exec_degraded={exec_degraded} reviewer_degraded={reviewer_degraded} "
            f"formatted_cmd_bytes={len(formatted_cmd.encode('utf-8'))}"
        ),
        passed=passed,
        evidence_paths=[
            _rel(dispatch_path),
            _rel(snapshot_path),
        ],
    )


def _case_t5_reviewer_strict_read_only() -> CaseResult:
    trace_id = f"trace-t5-{uuid4().hex[:8]}"
    task_id = f"task-t5-{uuid4().hex[:8]}"
    repo = _git_init_repo(ROOT / ".agn_workspace" / "event_driven" / "regression_repos", f"t5_repo_{uuid4().hex[:6]}")
    repo_ref = build_repo_ref(f"repo_{task_id}")
    register_repo_ref(repo_ref=repo_ref, repo_path=str(repo))
    baseline_status = (repo / ".git").exists()
    before = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        text=True,
        capture_output=True,
        check=False,
    ).stdout.strip()

    from action_protocol import build_action
    from event_sourcing import enqueue_action

    action = build_action(
        trace_id=trace_id,
        task_id=task_id,
        action_id=f"act-{uuid4().hex[:8]}",
        action_type="EXECUTE_CMD",
        inputs={
            "argv": ["git", "apply", "/dev/null"],
            "timeout_sec": 30,
            "attempt": 1,
            "execution_role": "reviewer",
        },
        refs={"repo_ref": repo_ref},
        budget={"max_time_sec": 30, "max_disk_mb": 64, "max_log_kb": 64},
    )
    enqueue_action(action)
    summary = run_pending(max_actions=10)
    events = load_events(trace_id)
    blocked_events = [e for e in events if e.get("event_type") == "ROLE_GUARD_BLOCKED"]
    finished = [e for e in events if e.get("event_type") == "ACTION_FINISHED"]
    after = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        text=True,
        capture_output=True,
        check=False,
    ).stdout.strip()
    rc = -1
    error_class = ""
    if finished:
        payload = finished[-1].get("payload", {}) or {}
        rc = int(payload.get("rc", -1) or -1)
        error_class = str(payload.get("error_class", ""))
    tree_clean = before == after
    passed = baseline_status and len(blocked_events) >= 1 and rc == 126 and error_class == "ROLE_GUARD_BLOCKED" and tree_clean
    return CaseResult(
        case_id="4.T5",
        section="Evo4 Reviewer Read-Only",
        command="EXECUTE_CMD(execution_role=reviewer, argv='git apply /dev/null')",
        expected="Role guard blocks reviewer write-capable command and emits ROLE_GUARD_BLOCKED; repo tree unchanged",
        actual=(
            f"errors={summary.get('errors')} blocked_events={len(blocked_events)} rc={rc} "
            f"error_class={error_class} tree_clean={tree_clean}"
        ),
        passed=passed,
        evidence_paths=[
            _rel(_events_path(trace_id)),
            _rel(repo),
        ],
    )


def _case_t6_evo5_suite() -> CaseResult:
    cmd = ["python3", "scripts/validation/run_evo5_regression.py"]
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    payload: dict[str, Any] = {}
    if proc.stdout.strip():
        try:
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception:
            payload = {}
    ok = proc.returncode == 0 and bool(payload.get("ok", False))
    json_report = str(payload.get("json_report", "")).strip()
    md_report = str(payload.get("markdown_report", "")).strip()
    evidence: list[str] = []
    if json_report:
        evidence.append(_rel(ROOT / json_report))
    if md_report:
        evidence.append(_rel(ROOT / md_report))
    return CaseResult(
        case_id="5.T1",
        section="Evo5 Unified Suite",
        command="python3 scripts/validation/run_evo5_regression.py",
        expected="Evo5 suite (Delivery Gate + Recovery/Escalation + Lifecycle) passes and emits reports",
        actual=f"rc={proc.returncode} ok={payload.get('ok')} totals={payload.get('totals')}",
        passed=ok,
        evidence_paths=evidence,
    )


def run_all_cases() -> list[CaseResult]:
    return [
        _case_t1_remote_separation(),
        _case_t2_read_by_action(),
        _case_t3_control_preemption(),
        _case_t4_large_text_invisibility(),
        _case_t5_reviewer_strict_read_only(),
        _case_t6_evo5_suite(),
    ]


def main() -> int:
    started = time.time()
    cases = run_all_cases()
    passed = sum(1 for c in cases if c.passed)
    failed = len(cases) - passed
    payload = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "duration_sec": round(time.time() - started, 3),
        "totals": {"all": len(cases), "passed": passed, "failed": failed},
        "cases": [asdict(c) for c in cases],
        "path_mode": "relative_to_repo_root",
        "repo_root": ".",
    }
    reports_dir = ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    output = reports_dir / f"event_driven_regression_{stamp}.json"
    output.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": failed == 0, "output": _rel(output), "totals": payload["totals"]}, ensure_ascii=True))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
