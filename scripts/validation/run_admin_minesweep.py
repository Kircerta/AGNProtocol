#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import time
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]

import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from agn_api.ssot_store import SSOTStore
from scripts.action_protocol import build_action
from scripts.action_runner import run_pending
from scripts.agn_refs import build_repo_ref
from scripts.coordinator_heartbeat import run_tick
from scripts.event_sourcing import (
    enqueue_action,
    enqueue_control_command,
    load_checkpoint,
    load_events,
    register_repo_ref,
    write_checkpoint,
)
from scripts.lifecycle_governance import integrity_sweep
from scripts.pointer_protocol import write_text_artifact


@dataclass
class TestResult:
    test_id: str
    category: str
    title: str
    expected: str
    status: str
    actual: str
    risk: str
    evidence: list[str]


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


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


def _new_task(*, task_id: str, trace_id: str, runner_cmd: list[str] | None = None, review_requested: bool = False) -> dict[str, Any]:
    return {
        "id": task_id,
        "source": "admin_minesweep",
        "request_text": "admin mine-sweep",
        "request_summary": "admin mine-sweep",
        "agn_managed": True,
        "review_requested": bool(review_requested),
        "decision": None,
        "status": "pending",
        "correlation_id": trace_id,
        "acceptance_criteria": [{"id": "AC-1", "text": "reach terminal"}],
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
        "runner_cmd": runner_cmd or ["echo", "ok"],
        "attempt": 1,
    }


def _run_task_until_terminal(*, task_id: str, backend: str = "remote_mock", max_ticks: int = 14) -> str:
    final = ""
    for _ in range(max_ticks):
        run_tick(max_tasks=2000, timeout_sec=60, task_filter=task_id, backend_name=backend)
        run_pending(max_actions=40)
        cp = load_checkpoint(task_id) or {}
        final = str(cp.get("state", ""))
        if final in {"DELIVERED", "ABORTED", "NEED_ADMIN"}:
            break
    return final


def _seed_repo(name: str) -> tuple[Path, str]:
    repo = ROOT / ".agn_workspace" / "event_driven" / "regression_repos" / f"{name}_{uuid4().hex[:6]}"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "README.md").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    repo_ref = build_repo_ref(f"repo_{name}_{uuid4().hex[:6]}")
    register_repo_ref(repo_ref=repo_ref, repo_path=str(repo))
    return repo, repo_ref


def t01_control_race_stop_wins(store: SSOTStore) -> TestResult:
    task_id = f"ms-t01-{uuid4().hex[:8]}"
    trace_id = f"ms-trace-t01-{uuid4().hex[:8]}"
    store.save_task(_new_task(task_id=task_id, trace_id=trace_id))

    enqueue_control_command({"control_id": f"ctl-pause-{uuid4().hex[:6]}", "control_type": "PAUSE", "task_id": task_id, "payload": {}})
    enqueue_control_command({"control_id": f"ctl-stop-{uuid4().hex[:6]}", "control_type": "STOP", "task_id": task_id, "payload": {}})
    enqueue_control_command({"control_id": f"ctl-resume-{uuid4().hex[:6]}", "control_type": "RESUME", "task_id": task_id, "payload": {}})

    run_tick(max_tasks=2000, timeout_sec=60, task_filter=task_id, backend_name="remote_mock")
    cp = load_checkpoint(task_id) or {}
    state = str(cp.get("state", ""))
    ok = state == "ABORTED"
    return TestResult(
        test_id="T01",
        category="Control Plane",
        title="PAUSE/STOP/RESUME 同批竞态",
        expected="STOP 应优先终止任务，不可被 RESUME 复活",
        status="PASS" if ok else "FAIL",
        actual=f"state={state}",
        risk="P0",
        evidence=[_rel(_events_path(trace_id))],
    )


def t02_modify_after_delivered_mutates(store: SSOTStore) -> TestResult:
    task_id = f"ms-t02-{uuid4().hex[:8]}"
    trace_id = f"ms-trace-t02-{uuid4().hex[:8]}"
    store.save_task(_new_task(task_id=task_id, trace_id=trace_id))
    final = _run_task_until_terminal(task_id=task_id)
    before = store.get_task(task_id) or {}
    before_ref = str(before.get("task_spec_ref", ""))

    enqueue_control_command(
        {
            "control_id": f"ctl-modify-{uuid4().hex[:6]}",
            "control_type": "MODIFY",
            "task_id": task_id,
            "payload": {"request_summary": "post-delivery mutate", "request_text": "post-delivery mutate text"},
        }
    )
    run_tick(max_tasks=2000, timeout_sec=60, task_filter=task_id, backend_name="remote_mock")
    after = store.get_task(task_id) or {}
    cp = load_checkpoint(task_id) or {}
    after_ref = str(after.get("task_spec_ref", ""))
    state = str(cp.get("state", ""))
    mutated = (after_ref != before_ref) and bool(after_ref)

    # 安全预期：DELIVERED 后不应允许 MODIFY 改写任务规格。
    ok = (final == "DELIVERED") and (not mutated)
    return TestResult(
        test_id="T02",
        category="Control Plane",
        title="DELIVERED 后 MODIFY 篡改",
        expected="DELIVERED 后应拒绝 MODIFY，不得改写 task_spec_ref",
        status="PASS" if ok else "FAIL",
        actual=f"pre_state={final} post_state={state} mutated={mutated} before_ref={before_ref} after_ref={after_ref}",
        risk="P0",
        evidence=[_rel(_events_path(trace_id))],
    )


def t03_duplicate_task_id_overwrite(store: SSOTStore) -> TestResult:
    task_id = f"ms-t03-fixed-{uuid4().hex[:6]}"
    trace_a = f"ms-trace-t03-a-{uuid4().hex[:6]}"
    trace_b = f"ms-trace-t03-b-{uuid4().hex[:6]}"
    first = _new_task(task_id=task_id, trace_id=trace_a, runner_cmd=["echo", "first"])
    second = _new_task(task_id=task_id, trace_id=trace_b, runner_cmd=["echo", "second"])
    store.save_task(first)
    conflict_blocked = False
    overwritten = False
    try:
        store.save_task(second)
        loaded = store.get_task(task_id) or {}
        overwritten = str(loaded.get("correlation_id", "")) == trace_b
    except ValueError:
        conflict_blocked = True

    # 安全预期：重复 task_id 应被拒绝或版本化。
    ok = conflict_blocked and (not overwritten)
    return TestResult(
        test_id="T03",
        category="Input Safety",
        title="重复 task_id 静默覆盖",
        expected="重复 task_id 需要拒绝或冲突提示",
        status="PASS" if ok else "FAIL",
        actual=f"conflict_blocked={conflict_blocked} overwritten={overwritten}",
        risk="P1",
        evidence=[_rel(ROOT / "ssot" / f"{task_id}.json")],
    )


def t04_invalid_modify_payload_rejected(store: SSOTStore) -> TestResult:
    task_id = f"ms-t04-{uuid4().hex[:8]}"
    trace_id = f"ms-trace-t04-{uuid4().hex[:8]}"
    store.save_task(_new_task(task_id=task_id, trace_id=trace_id))
    enqueue_control_command(
        {
            "control_id": f"ctl-badmod-{uuid4().hex[:6]}",
            "control_type": "MODIFY",
            "task_id": task_id,
            "payload": ["not", "a", "dict"],
        }
    )
    run_tick(max_tasks=2000, timeout_sec=60, task_filter=task_id, backend_name="remote_mock")
    events = load_events(trace_id)
    rejected = any(e.get("event_type") == "CONTROL_REJECTED" for e in events)
    return TestResult(
        test_id="T04",
        category="Input Safety",
        title="非法 MODIFY payload",
        expected="应拒绝并写 CONTROL_REJECTED 事件",
        status="PASS" if rejected else "FAIL",
        actual=f"control_rejected={rejected}",
        risk="P1",
        evidence=[_rel(_events_path(trace_id))],
    )


def t05_malformed_action_rejected() -> TestResult:
    trace_id = f"ms-trace-t05-{uuid4().hex[:8]}"
    task_id = f"ms-t05-{uuid4().hex[:8]}"
    enqueue_action({"trace_id": trace_id, "task_id": task_id, "action_id": f"bad-{uuid4().hex[:6]}"})
    summary = run_pending(max_actions=10)
    events = load_events(trace_id)
    violation = any(e.get("event_type") == "PROTOCOL_VIOLATION" for e in events)
    ok = int(summary.get("errors", 0) or 0) >= 1 and violation
    return TestResult(
        test_id="T05",
        category="Protocol",
        title="畸形 action 注入",
        expected="Runner 必须拒绝并记录 PROTOCOL_VIOLATION",
        status="PASS" if ok else "FAIL",
        actual=f"errors={summary.get('errors')} protocol_violation={violation}",
        risk="P0",
        evidence=[_rel(_events_path(trace_id))],
    )


def t06_reviewer_python_write_blocked() -> TestResult:
    repo, repo_ref = _seed_repo("ms_t06")
    trace_id = f"ms-trace-t06-{uuid4().hex[:8]}"
    task_id = f"ms-t06-{uuid4().hex[:8]}"
    target = repo / "pwn.txt"

    action = build_action(
        trace_id=trace_id,
        task_id=task_id,
        action_id=f"act-{uuid4().hex[:8]}",
        action_type="EXECUTE_CMD",
        inputs={
            "argv": ["python3", "-c", f"open(r'{str(target)}','w').write('x')"],
            "execution_role": "reviewer",
            "timeout_sec": 20,
            "attempt": 1,
        },
        refs={"repo_ref": repo_ref},
        budget={"max_time_sec": 30, "max_disk_mb": 32, "max_log_kb": 32},
    )
    enqueue_action(action)
    summary = run_pending(max_actions=10)
    events = load_events(trace_id)
    blocked = any(e.get("event_type") == "ROLE_GUARD_BLOCKED" for e in events)
    no_write = not target.exists()
    ok = blocked and no_write and int(summary.get("errors", 0) or 0) >= 1
    return TestResult(
        test_id="T06",
        category="Role Guard",
        title="Reviewer 通过 python -c 写文件",
        expected="必须拦截，repo 不得新增文件",
        status="PASS" if ok else "FAIL",
        actual=f"blocked={blocked} no_write={no_write} errors={summary.get('errors')}",
        risk="P0",
        evidence=[_rel(_events_path(trace_id)), _rel(repo)],
    )


def t07_read_repo_path_traversal_blocked() -> TestResult:
    _repo, repo_ref = _seed_repo("ms_t07")
    trace_id = f"ms-trace-t07-{uuid4().hex[:8]}"
    task_id = f"ms-t07-{uuid4().hex[:8]}"
    action = build_action(
        trace_id=trace_id,
        task_id=task_id,
        action_id=f"act-{uuid4().hex[:8]}",
        action_type="READ_REPO_FILE",
        inputs={
            "path": "../../../../etc/passwd",
            "max_bytes": 2048,
            "need_summary": True,
            "need_excerpt": True,
        },
        refs={"repo_ref": repo_ref},
        budget={"max_time_sec": 30, "max_disk_mb": 8, "max_log_kb": 32},
    )
    enqueue_action(action)
    run_pending(max_actions=10)
    events = load_events(trace_id)
    finished = [e for e in events if e.get("event_type") == "ACTION_FINISHED"]
    rc = -1
    error_class = ""
    if finished:
        payload = finished[-1].get("payload", {}) or {}
        rc = int(payload.get("rc", -1) or -1)
        error_class = str(payload.get("error_class", ""))
    ok = rc != 0 and ("READ" in error_class or "INVALID" in error_class or "ROLE_GUARD" in error_class)
    return TestResult(
        test_id="T07",
        category="Read Channel",
        title="READ_REPO_FILE 路径穿越",
        expected="必须拒绝读取 repo 外路径",
        status="PASS" if ok else "FAIL",
        actual=f"rc={rc} error_class={error_class}",
        risk="P0",
        evidence=[_rel(_events_path(trace_id))],
    )


def t08_read_ref_budget_clamp() -> TestResult:
    trace_id = f"ms-trace-t08-{uuid4().hex[:8]}"
    task_id = f"ms-t08-{uuid4().hex[:8]}"
    artifact = write_text_artifact(
        task_id=task_id,
        attempt=1,
        artifact_id="big",
        content="Z" * 16000,
        media_type="text/plain",
        filename="big.txt",
        source="admin_minesweep",
    )
    action = build_action(
        trace_id=trace_id,
        task_id=task_id,
        action_id=f"act-{uuid4().hex[:8]}",
        action_type="READ_REF",
        inputs={"max_bytes": 10_000_000, "need_summary": True, "need_excerpt": True},
        refs={"ref": artifact.ref},
        budget={"max_time_sec": 30, "max_disk_mb": 8, "max_log_kb": 1},
    )
    enqueue_action(action)
    run_pending(max_actions=10)
    events = load_events(trace_id)
    read_events = [e for e in events if e.get("event_type") == "READ_RESULT_CREATED"]
    max_bytes = -1
    truncated = False
    if read_events:
        payload = read_events[-1].get("payload", {}) or {}
        max_bytes = int(payload.get("max_bytes", -1) or -1)
        truncated = bool(payload.get("truncated", False))
    ok = (0 < max_bytes <= 1024) and truncated
    return TestResult(
        test_id="T08",
        category="Read Channel",
        title="READ_REF 超大 max_bytes 注入",
        expected="应按预算钳制读取大小并标记 truncated",
        status="PASS" if ok else "FAIL",
        actual=f"max_bytes={max_bytes} truncated={truncated}",
        risk="P1",
        evidence=[_rel(_events_path(trace_id))],
    )


def t09_event_log_corruption_tolerated(store: SSOTStore) -> TestResult:
    task_id = f"ms-t09-{uuid4().hex[:8]}"
    trace_id = f"ms-trace-t09-{uuid4().hex[:8]}"
    store.save_task(_new_task(task_id=task_id, trace_id=trace_id))
    _run_task_until_terminal(task_id=task_id)
    ep = _events_path(trace_id)
    with ep.open("a", encoding="utf-8") as handle:
        handle.write("{this-is-not-json}\n")
    crashed = False
    try:
        run_tick(max_tasks=2000, timeout_sec=60, task_filter=task_id, backend_name="remote_mock")
    except Exception:
        crashed = True
    ok = not crashed
    return TestResult(
        test_id="T09",
        category="SSOT Robustness",
        title="事件流损坏容忍",
        expected="损坏行应被跳过，心跳不中断",
        status="PASS" if ok else "FAIL",
        actual=f"heartbeat_crashed={crashed}",
        risk="P1",
        evidence=[_rel(ep)],
    )


def t10_invalid_checkpoint_state_detected(store: SSOTStore) -> TestResult:
    task_id = f"ms-t10-{uuid4().hex[:8]}"
    trace_id = f"ms-trace-t10-{uuid4().hex[:8]}"
    store.save_task(_new_task(task_id=task_id, trace_id=trace_id))
    write_checkpoint(task_id, {"task_id": task_id, "trace_id": trace_id, "state": "HACKED", "paused": False})
    run_tick(max_tasks=2000, timeout_sec=60, task_filter=task_id, backend_name="remote_mock")
    cp = load_checkpoint(task_id) or {}
    events = load_events(trace_id)
    state = str(cp.get("state", ""))
    violation = any(e.get("event_type") == "PROTOCOL_VIOLATION" for e in events)

    # 安全预期：非法状态应被检测并回收为合法状态或触发 violation。
    ok = (state in {"CREATED", "PLANNED", "ABORTED", "NEED_ADMIN", "DELIVERED", "DISPATCHED_EXEC", "EXEC_RUNNING", "EXEC_DONE", "DISPATCHED_REVIEW", "REVIEW_RUNNING", "REVIEW_DONE", "SYNTHESIS", "DELIVERY_GATE"}) and violation
    return TestResult(
        test_id="T10",
        category="State Machine",
        title="checkpoint 非法状态注入",
        expected="应检测非法 state 并产生告警/纠正",
        status="PASS" if ok else "FAIL",
        actual=f"state={state} protocol_violation={violation}",
        risk="P0",
        evidence=[_rel(_events_path(trace_id))],
    )


def t11_tool_error_not_silent(store: SSOTStore) -> TestResult:
    task_id = f"ms-t11-{uuid4().hex[:8]}"
    trace_id = f"ms-trace-t11-{uuid4().hex[:8]}"
    store.save_task(_new_task(task_id=task_id, trace_id=trace_id, runner_cmd=["definitely_nonexistent_binary_agn", "x"]))

    for _ in range(6):
        run_tick(max_tasks=2000, timeout_sec=60, task_filter=task_id, backend_name="remote_mock")
        run_pending(max_actions=20)

    events = load_events(trace_id)
    finished_fail = [
        e
        for e in events
        if e.get("event_type") == "ACTION_FINISHED"
        and int(((e.get("payload", {}) or {}).get("rc", 0) or 0)) != 0
    ]
    recovery_planned = [
        e
        for e in events
        if e.get("event_type") == "ACTION_PLANNED"
        and str((e.get("payload", {}) or {}).get("action_type", "")) in {"RETRY", "SUMMARIZE"}
    ]
    cp = load_checkpoint(task_id) or {}
    state = str(cp.get("state", ""))
    ok = bool(finished_fail) and (bool(recovery_planned) or state == "NEED_ADMIN")
    return TestResult(
        test_id="T11",
        category="Recovery",
        title="工具执行失败后的静默停机",
        expected="失败后应进入恢复分支（RETRY/DEGRADE/NEED_ADMIN），不可静默",
        status="PASS" if ok else "FAIL",
        actual=f"failed_actions={len(finished_fail)} recovery_actions={len(recovery_planned)} state={state}",
        risk="P0",
        evidence=[_rel(_events_path(trace_id))],
    )


def t12_invalid_evidence_blocks_delivery(store: SSOTStore) -> TestResult:
    task_id = f"ms-t12-{uuid4().hex[:8]}"
    trace_id = f"ms-trace-t12-{uuid4().hex[:8]}"
    task = _new_task(task_id=task_id, trace_id=trace_id)
    bad_spec = {
        "task_id": task_id,
        "trace_id": trace_id,
        "blocking": True,
        "items": [
            {
                "ac_id": "AC-BAD",
                "statement": "invalid evidence",
                "evidence_type": "log_ref",
                "required": True,
                "evidence_refs": ["agn://artifact/" + ("0" * 64)],
                "validator": {"kind": "contains", "needle": "must-not-exist"},
            }
        ],
    }
    ref = write_text_artifact(
        task_id=task_id,
        attempt=1,
        artifact_id="bad_spec",
        content=json.dumps(bad_spec, ensure_ascii=True),
        media_type="application/json",
        filename="bad_spec.json",
        source="admin_minesweep",
    ).ref
    task["acceptance_spec_ref"] = ref
    store.save_task(task)
    write_checkpoint(task_id, {"task_id": task_id, "trace_id": trace_id, "state": "DELIVERY_GATE", "paused": False})
    run_tick(max_tasks=2000, timeout_sec=60, task_filter=task_id, backend_name="remote_mock")
    cp = load_checkpoint(task_id) or {}
    state = str(cp.get("state", ""))
    events = load_events(trace_id)
    gate_failed = any(e.get("event_type") == "DELIVERY_GATE_FAILED" for e in events)
    ok = state != "DELIVERED" and gate_failed
    return TestResult(
        test_id="T12",
        category="Delivery Gate",
        title="伪造证据绕过交付",
        expected="无效证据必须阻止 DELIVERED",
        status="PASS" if ok else "FAIL",
        actual=f"state={state} gate_failed={gate_failed}",
        risk="P0",
        evidence=[_rel(_events_path(trace_id))],
    )


def t13_trace_collision_contamination(store: SSOTStore) -> TestResult:
    shared_trace = f"ms-shared-trace-{uuid4().hex[:8]}"
    task_a = f"ms-t13-a-{uuid4().hex[:6]}"
    task_b = f"ms-t13-b-{uuid4().hex[:6]}"
    store.save_task(_new_task(task_id=task_a, trace_id=shared_trace, runner_cmd=["echo", "A"]))
    store.save_task(_new_task(task_id=task_b, trace_id=shared_trace, runner_cmd=["echo", "B"]))

    _run_task_until_terminal(task_id=task_a)
    _run_task_until_terminal(task_id=task_b)

    events = load_events(shared_trace)
    task_ids = {str(e.get("task_id", "")) for e in events if isinstance(e, dict)}
    mixed = task_a in task_ids and task_b in task_ids

    # 安全预期：trace 粒度至少应与 task 一致，避免多 task 共享 trace 导致证据污染。
    ok = not mixed
    return TestResult(
        test_id="T13",
        category="Isolation",
        title="同 correlation_id 导致 trace 污染",
        expected="不同 task 不应混入同一 trace 事件流",
        status="PASS" if ok else "FAIL",
        actual=f"mixed_trace={mixed} task_ids={sorted(task_ids)}",
        risk="P0",
        evidence=[_rel(_events_path(shared_trace))],
    )


def _trace_ids_from_results(results: list[TestResult]) -> set[str]:
    trace_ids: set[str] = set()
    for result in results:
        for ev in result.evidence:
            p = Path(str(ev))
            if p.suffix != ".jsonl":
                continue
            if "events" not in p.parts:
                continue
            trace = p.stem.strip()
            if trace:
                trace_ids.add(trace)
    return trace_ids


def t14_integrity_baseline(*, trace_scope: set[str]) -> TestResult:
    out = integrity_sweep()
    missing = int(out.get("missing_count", 0) or 0)
    missing_refs = out.get("missing_refs", []) if isinstance(out.get("missing_refs"), list) else []
    scoped_missing = [
        item
        for item in missing_refs
        if isinstance(item, dict) and str(item.get("trace_id", "")).strip() in trace_scope
    ]
    scoped_missing_count = len(scoped_missing)
    ok = scoped_missing_count == 0
    report = str(out.get("report", "")).strip()
    return TestResult(
        test_id="T14",
        category="Integrity",
        title="基线完整性巡检",
        expected="本轮实验 trace_scope 内 missing_count=0",
        status="PASS" if ok else "FAIL",
        actual=f"scoped_missing_count={scoped_missing_count} global_missing_count={missing}",
        risk="P0",
        evidence=[report] if report else [],
    )


def _recommendation(test_id: str) -> str:
    recs = {
        "T02": "在 CONTROL MODIFY 入口增加终态拒绝（DELIVERED/ABORTED/NEED_ADMIN 不可改写 task_spec）。",
        "T03": "在 save_task 增加 task_id 冲突检测与幂等键，禁止静默覆盖。",
        "T10": "心跳入口校验 checkpoint.state，不合法时写 PROTOCOL_VIOLATION 并强制回收到 PLANNED/NEED_ADMIN。",
        "T13": "约束 correlation_id 唯一绑定 task_id，或 trace_id 改为 task_id 优先，禁止多 task 共 trace。",
        "T14": "建立定期清理/修复流程，保证基线环境下 integrity_sweep 可为 0 missing。",
    }
    return recs.get(test_id, "维持当前防线并增加对应回归覆盖。")


def main() -> int:
    started = time.time()
    day = date.today().isoformat()
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")

    report_dir = ROOT / "reports" / f"admin_minesweep_{stamp}"
    report_dir.mkdir(parents=True, exist_ok=True)

    # 先尽量清空 pending，减少外部残留干扰。
    for _ in range(4):
        run_pending(max_actions=200)

    store = SSOTStore(ROOT / "ssot")

    tests = [
        t01_control_race_stop_wins,
        t02_modify_after_delivered_mutates,
        t03_duplicate_task_id_overwrite,
        t04_invalid_modify_payload_rejected,
        t05_malformed_action_rejected,
        t06_reviewer_python_write_blocked,
        t07_read_repo_path_traversal_blocked,
        t08_read_ref_budget_clamp,
        t09_event_log_corruption_tolerated,
        t10_invalid_checkpoint_state_detected,
        t11_tool_error_not_silent,
        t12_invalid_evidence_blocks_delivery,
        t13_trace_collision_contamination,
    ]

    results: list[TestResult] = []
    for fn in tests:
        try:
            if fn.__name__ in {
                "t05_malformed_action_rejected",
                "t06_reviewer_python_write_blocked",
                "t07_read_repo_path_traversal_blocked",
                "t08_read_ref_budget_clamp",
                "t14_integrity_baseline",
            }:
                result = fn()  # type: ignore[misc]
            else:
                result = fn(store)  # type: ignore[misc]
        except Exception as exc:
            result = TestResult(
                test_id=fn.__name__.upper(),
                category="Execution",
                title=fn.__name__,
                expected="测试应可执行",
                status="FAIL",
                actual=f"exception={type(exc).__name__}:{exc}",
                risk="P0",
                evidence=[],
            )
        results.append(result)

    fail_items = [r for r in results if r.status != "PASS"]
    p0_fail = [r for r in fail_items if r.risk == "P0"]

    # T14 should evaluate only artifacts referenced by this run, while still reporting global missing count.
    trace_scope = _trace_ids_from_results(results)
    try:
        results.append(t14_integrity_baseline(trace_scope=trace_scope))
    except Exception as exc:
        results.append(
            TestResult(
                test_id="T14_INTEGRITY_BASELINE",
                category="Execution",
                title="t14_integrity_baseline",
                expected="测试应可执行",
                status="FAIL",
                actual=f"exception={type(exc).__name__}:{exc}",
                risk="P0",
                evidence=[],
            )
        )

    summary = {
        "generated_at": _utc_now_iso(),
        "duration_sec": round(time.time() - started, 3),
        "totals": {
            "all": len(results),
            "pass": sum(1 for r in results if r.status == "PASS"),
            "fail": len(fail_items),
            "p0_fail": len(p0_fail),
        },
        "verdict": "HIGH_RISK" if p0_fail else ("RISK_PRESENT" if fail_items else "CLEAN"),
        "results": [asdict(r) for r in results],
    }

    summary_path = ROOT / "documentation" / "admin" / "AGN1.0_Admin_Minesweep_Summary.json"
    index_path = ROOT / "documentation" / "admin" / "AGN1.0_Admin_Minesweep_Artifacts_Index.json"
    report_path = ROOT / "documentation" / "admin" / f"AGN1.0_Admin_Minesweep_Report_{day}.md"

    summary_path.write_text(json.dumps(summary, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    index_payload = {
        "generated_at": _utc_now_iso(),
        "reports_root": _rel(report_dir),
        "items": [
            {
                "test_id": r.test_id,
                "status": r.status,
                "risk": r.risk,
                "evidence": r.evidence,
            }
            for r in results
        ],
    }
    index_path.write_text(json.dumps(index_payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    lines: list[str] = []
    lines.append(f"# AGN1.0 Admin Mine-Sweep Report ({day})")
    lines.append("")
    lines.append("## Context")
    lines.append("- Method: 假设系统到处有漏洞，以 Admin 实际使用路径做故障注入与边界破坏实验。")
    lines.append(f"- Repo Root: `{_rel(ROOT)}`")
    lines.append(f"- Reports Root: `{_rel(report_dir)}`")
    lines.append("")
    lines.append("## Overall")
    lines.append(f"- verdict: `{summary['verdict']}`")
    lines.append(f"- total: `{summary['totals']['all']}`")
    lines.append(f"- pass: `{summary['totals']['pass']}`")
    lines.append(f"- fail: `{summary['totals']['fail']}`")
    lines.append(f"- p0_fail: `{summary['totals']['p0_fail']}`")
    lines.append("")
    lines.append("## Results")
    lines.append("")

    for r in results:
        lines.append(f"### {r.test_id} [{r.risk}] {r.title}")
        lines.append(f"- category: {r.category}")
        lines.append(f"- expected: {r.expected}")
        lines.append(f"- status: {r.status}")
        lines.append(f"- actual: {r.actual}")
        lines.append(f"- evidence: {', '.join(r.evidence) if r.evidence else '(none)'}")
        if r.status != "PASS":
            lines.append(f"- suggested_fix: {_recommendation(r.test_id)}")
        lines.append("")

    lines.append("## Key Risk Notes")
    if p0_fail:
        for r in p0_fail:
            lines.append(f"- {r.test_id}: {r.title} -> {r.actual}")
    else:
        lines.append("- No P0 failures in this run.")
    lines.append("")
    lines.append("## Output Files")
    lines.append(f"- `{_rel(report_path)}`")
    lines.append(f"- `{_rel(summary_path)}`")
    lines.append(f"- `{_rel(index_path)}`")

    report_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "ok": len(p0_fail) == 0,
                "verdict": summary["verdict"],
                "report": _rel(report_path),
                "summary": _rel(summary_path),
                "artifacts_index": _rel(index_path),
                "totals": summary["totals"],
            },
            ensure_ascii=True,
        )
    )

    return 0 if len(p0_fail) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
