from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from agn_api.ssot_store import SSOTStore
from scripts.agn_refs import build_object_ref, build_repo_ref
from scripts.coordinator_heartbeat import run_tick
from scripts.event_sourcing import append_event, cancel_pending_actions, load_checkpoint, load_events, write_checkpoint
from scripts.pointer_protocol import write_json_artifact, write_text_artifact

ROOT = Path(__file__).resolve().parents[1]


def _store() -> SSOTStore:
    return SSOTStore(ROOT / "ssot")


def _base_task(*, task_id: str, trace_id: str, review_requested: bool = True) -> dict[str, object]:
    return {
        "id": task_id,
        "source": "test_evo5_delivery_gate",
        "request_text": "evo5 delivery gate task",
        "request_summary": "evo5 delivery gate task",
        "agn_managed": True,
        "review_requested": review_requested,
        "decision": None,
        "status": "pending",
        "correlation_id": trace_id,
        "acceptance_criteria": [{"id": "AC-1", "text": "must have evidence"}],
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
        "runner_cmd": ["echo", "gate-ok"],
        "attempt": 1,
    }


def _write_spec(*, task_id: str, trace_id: str, items: list[dict[str, object]]) -> str:
    artifact = write_json_artifact(
        task_id=task_id,
        attempt=1,
        artifact_id=f"acceptance_spec_test_{uuid4().hex[:6]}",
        payload={
            "task_id": task_id,
            "trace_id": trace_id,
            "blocking": True,
            "items": items,
        },
        filename=f"acceptance_spec_{uuid4().hex[:6]}.json",
        source="test",
    )
    return artifact.ref


def test_delivery_gate_blocks_without_evidence() -> None:
    task_id = f"task-evo5-gate-block-{uuid4().hex[:8]}"
    trace_id = f"trace-evo5-gate-block-{uuid4().hex[:8]}"
    store = _store()
    task = _base_task(task_id=task_id, trace_id=trace_id, review_requested=True)
    task["acceptance_spec_ref"] = _write_spec(
        task_id=task_id,
        trace_id=trace_id,
        items=[
            {
                "ac_id": "AC-RESULT",
                "statement": "result evidence required",
                "evidence_type": "result_ref",
                "required": True,
                "evidence_refs": [],
            },
            {
                "ac_id": "AC-VERDICT",
                "statement": "verdict evidence required",
                "evidence_type": "verdict_ref",
                "required": True,
                "evidence_refs": [],
            },
        ],
    )
    store.save_task(task)
    write_checkpoint(task_id, {"task_id": task_id, "trace_id": trace_id, "state": "DELIVERY_GATE", "paused": False})

    run_tick(max_tasks=2000, timeout_sec=60, task_filter=task_id, backend_name="remote_mock")

    cp = load_checkpoint(task_id) or {}
    assert str(cp.get("state", "")) != "DELIVERED"

    events = load_events(trace_id)
    assert any(e.get("event_type") == "DELIVERY_GATE_FAILED" for e in events)
    cancel_pending_actions(trace_id=trace_id, task_id=task_id, reason="test_cleanup")


def test_delivery_gate_passes_with_evidence() -> None:
    task_id = f"task-evo5-gate-pass-{uuid4().hex[:8]}"
    trace_id = f"trace-evo5-gate-pass-{uuid4().hex[:8]}"
    store = _store()
    task = _base_task(task_id=task_id, trace_id=trace_id, review_requested=True)

    result_ref = build_object_ref("result", trace_id, 1)
    verdict_ref = build_object_ref("verdict", trace_id, 1)
    (ROOT / "results").mkdir(parents=True, exist_ok=True)
    (ROOT / "verdicts").mkdir(parents=True, exist_ok=True)
    (ROOT / "results" / f"{task_id}.1.json").write_text(json.dumps({"ok": True}) + "\n", encoding="utf-8")
    (ROOT / "verdicts" / f"{task_id}.1.json").write_text(json.dumps({"decision": "approve"}) + "\n", encoding="utf-8")

    task["acceptance_spec_ref"] = _write_spec(
        task_id=task_id,
        trace_id=trace_id,
        items=[
            {
                "ac_id": "AC-RESULT",
                "statement": "result evidence required",
                "evidence_type": "result_ref",
                "required": True,
                "evidence_refs": [result_ref],
            },
            {
                "ac_id": "AC-VERDICT",
                "statement": "verdict evidence required",
                "evidence_type": "verdict_ref",
                "required": True,
                "evidence_refs": [verdict_ref],
            },
        ],
    )
    store.save_task(task)
    write_checkpoint(task_id, {"task_id": task_id, "trace_id": trace_id, "state": "DELIVERY_GATE", "paused": False})

    run_tick(max_tasks=2000, timeout_sec=60, task_filter=task_id, backend_name="remote_mock")

    cp = load_checkpoint(task_id) or {}
    assert str(cp.get("state", "")) == "DELIVERED"


def test_delivery_gate_loopback_generates_actions() -> None:
    task_id = f"task-evo5-gate-loop-{uuid4().hex[:8]}"
    trace_id = f"trace-evo5-gate-loop-{uuid4().hex[:8]}"
    store = _store()
    task = _base_task(task_id=task_id, trace_id=trace_id, review_requested=True)

    result_ref = build_object_ref("result", trace_id, 1)
    (ROOT / "results").mkdir(parents=True, exist_ok=True)
    (ROOT / "results" / f"{task_id}.1.json").write_text(json.dumps({"ok": True}) + "\n", encoding="utf-8")

    task["acceptance_spec_ref"] = _write_spec(
        task_id=task_id,
        trace_id=trace_id,
        items=[
            {
                "ac_id": "AC-RESULT",
                "statement": "result evidence required",
                "evidence_type": "result_ref",
                "required": True,
                "evidence_refs": [result_ref],
            },
            {
                "ac_id": "AC-VERDICT",
                "statement": "verdict evidence required",
                "evidence_type": "verdict_ref",
                "required": True,
                "evidence_refs": [],
            },
        ],
    )
    store.save_task(task)
    write_checkpoint(task_id, {"task_id": task_id, "trace_id": trace_id, "state": "DELIVERY_GATE", "paused": False})

    run_tick(max_tasks=2000, timeout_sec=60, task_filter=task_id, backend_name="remote_mock")

    events = load_events(trace_id)
    planned = [
        e for e in events
        if e.get("event_type") == "ACTION_PLANNED"
        and str((e.get("payload", {}) or {}).get("action_type", "")) == "REQUEST_REVIEW"
    ]
    assert planned
    assert not any(
        e.get("event_type") == "ACTION_PLANNED"
        and str((e.get("payload", {}) or {}).get("action_type", "")) == "EXECUTE_CMD"
        for e in events
    )
    cp = load_checkpoint(task_id) or {}
    assert str(cp.get("state", "")) == "DISPATCHED_REVIEW"
    cancel_pending_actions(trace_id=trace_id, task_id=task_id, reason="test_cleanup")


def test_delivery_gate_log_ref_requires_execute_cmd_evidence() -> None:
    task_id = f"task-evo5-gate-logexec-{uuid4().hex[:8]}"
    trace_id = f"trace-evo5-gate-logexec-{uuid4().hex[:8]}"
    store = _store()
    task = _base_task(task_id=task_id, trace_id=trace_id, review_requested=False)
    task["acceptance_spec_ref"] = _write_spec(
        task_id=task_id,
        trace_id=trace_id,
        items=[
            {
                "ac_id": "AC-EXEC-ONLY",
                "statement": "execution evidence required",
                "evidence_type": "log_ref",
                "required": True,
                "evidence_refs": [],
            }
        ],
    )
    store.save_task(task)
    write_checkpoint(task_id, {"task_id": task_id, "trace_id": trace_id, "state": "DELIVERY_GATE", "paused": False})

    summary_ref = write_text_artifact(
        task_id=task_id,
        attempt=1,
        artifact_id="summary_only",
        content="summary-only output",
        media_type="text/plain",
        filename="summary_only.txt",
        source="test",
    )
    append_event(
        trace_id=trace_id,
        task_id=task_id,
        event_type="ACTION_FINISHED",
        action_id=f"sum-{uuid4().hex[:6]}",
        payload={
            "action_type": "SUMMARIZE",
            "rc": 0,
            "result_ref": summary_ref.ref,
        },
    )

    run_tick(max_tasks=2000, timeout_sec=60, task_filter=task_id, backend_name="remote_mock")

    cp = load_checkpoint(task_id) or {}
    assert str(cp.get("state", "")) != "DELIVERED"
    events = load_events(trace_id)
    assert any(e.get("event_type") == "DELIVERY_GATE_FAILED" for e in events)
    cancel_pending_actions(trace_id=trace_id, task_id=task_id, reason="test_cleanup")
