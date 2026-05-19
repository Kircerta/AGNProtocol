#!/usr/bin/env python3
from __future__ import annotations

from typing import Any
from uuid import uuid4

try:
    from action_protocol import build_action
    from agn_refs import build_object_ref
except ImportError:  # pragma: no cover - package import fallback
    from scripts.action_protocol import build_action
    from scripts.agn_refs import build_object_ref

DEFAULT_BUDGET = {"max_time_sec": 900, "max_disk_mb": 512, "max_log_kb": 512}


def _has_pending(snapshot: dict[str, Any], action_type: str) -> bool:
    for item in snapshot.get("pending_actions", []) or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("action_type", "")).strip() == str(action_type).strip():
            return True
    return False


def _next_action_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def _task_spec(snapshot: dict[str, Any]) -> dict[str, Any]:
    raw = snapshot.get("task_spec", {})
    return raw if isinstance(raw, dict) else {}


def _budget(snapshot: dict[str, Any]) -> dict[str, Any]:
    raw = snapshot.get("perf_budget", {})
    if not isinstance(raw, dict):
        return dict(DEFAULT_BUDGET)
    return {
        "max_time_sec": float(raw.get("max_time_sec", DEFAULT_BUDGET["max_time_sec"]) or DEFAULT_BUDGET["max_time_sec"]),
        "max_disk_mb": float(raw.get("max_disk_mb", DEFAULT_BUDGET["max_disk_mb"]) or DEFAULT_BUDGET["max_disk_mb"]),
        "max_log_kb": float(raw.get("max_log_kb", DEFAULT_BUDGET["max_log_kb"]) or DEFAULT_BUDGET["max_log_kb"]),
    }


def _plan_exec_action(snapshot: dict[str, Any]) -> dict[str, Any]:
    spec = _task_spec(snapshot)
    task_id = str(snapshot.get("task_id", "")).strip()
    trace_id = str(snapshot.get("trace_id", "")).strip()
    attempt = int(spec.get("attempt", 1) or 1)
    runner_cmd = spec.get("runner_cmd")
    argv: list[str]
    if isinstance(runner_cmd, list) and runner_cmd:
        argv = [str(item) for item in runner_cmd]
    else:
        argv = ["echo", f"execute task {task_id}"]
    refs: dict[str, Any] = {
        "instruction_ref": str(spec.get("request_text_ref", "")).strip(),
        "task_spec_ref": str(spec.get("task_spec_ref", "")).strip(),
    }
    repo_ref = str(spec.get("repo_ref", "")).strip()
    if repo_ref:
        refs["repo_ref"] = repo_ref
    # Filter empty refs so validator never sees empty placeholders.
    refs = {k: v for k, v in refs.items() if str(v).strip()}

    return build_action(
        trace_id=trace_id,
        task_id=task_id,
        action_id=_next_action_id("exec"),
        action_type="EXECUTE_CMD",
        inputs={"argv": argv, "attempt": attempt, "timeout_sec": 900},
        refs=refs,
        budget=_budget(snapshot),
        source_role="coordinator",
        state_hint="DISPATCHED_EXEC",
    )


def _plan_review_action(snapshot: dict[str, Any]) -> dict[str, Any]:
    spec = _task_spec(snapshot)
    task_id = str(snapshot.get("task_id", "")).strip()
    trace_id = str(snapshot.get("trace_id", "")).strip()
    attempt = int(spec.get("attempt", 1) or 1)
    return build_action(
        trace_id=trace_id,
        task_id=task_id,
        action_id=_next_action_id("review"),
        action_type="REQUEST_REVIEW",
        inputs={"attempt": attempt},
        refs={
            "dispatch_ref": build_object_ref("dispatch", trace_id, attempt),
            "result_ref": build_object_ref("result", trace_id, attempt),
            "verdict_ref": build_object_ref("verdict", trace_id, attempt),
        },
        budget=_budget(snapshot),
        source_role="coordinator",
        state_hint="DISPATCHED_REVIEW",
    )


def _plan_read_context_action(snapshot: dict[str, Any], *, use_ref: bool) -> dict[str, Any]:
    spec = _task_spec(snapshot)
    task_id = str(snapshot.get("task_id", "")).strip()
    trace_id = str(snapshot.get("trace_id", "")).strip()
    attempt = int(spec.get("attempt", 1) or 1)
    if use_ref:
        return build_action(
            trace_id=trace_id,
            task_id=task_id,
            action_id=_next_action_id("readref"),
            action_type="READ_REF",
            inputs={
                "attempt": attempt,
                "max_bytes": 4096,
                "need_summary": True,
                "need_excerpt": True,
            },
            refs={"ref": str(spec.get("request_text_ref", "")).strip()},
            budget=_budget(snapshot),
            source_role="coordinator",
            state_hint="PLANNED",
        )

    refs: dict[str, Any] = {}
    repo_ref = str(spec.get("repo_ref", "")).strip()
    if repo_ref:
        refs["repo_ref"] = repo_ref
    return build_action(
        trace_id=trace_id,
        task_id=task_id,
        action_id=_next_action_id("readrepo"),
        action_type="READ_REPO_FILE",
        inputs={
            "attempt": attempt,
            "path": str(spec.get("context_read_path", "README.md")).strip() or "README.md",
            "max_bytes": 4096,
            "need_summary": True,
            "need_excerpt": True,
            "line_range": {"start": 1, "end": 200},
        },
        refs=refs,
        budget=_budget(snapshot),
        source_role="coordinator",
        state_hint="PLANNED",
    )


def propose_actions(
    *,
    snapshot: dict[str, Any],
    recent_event_digests: list[dict[str, Any]],
    control_commands: list[dict[str, Any]],
    ref_index: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    del control_commands
    del ref_index
    actions: list[dict[str, Any]] = []
    state = str(snapshot.get("state", "")).strip().upper()
    paused = bool(snapshot.get("paused", False))
    spec = _task_spec(snapshot)
    if paused:
        return actions

    if state == "PLANNED":
        needs_context_read = bool(spec.get("needs_context_read", False))
        context_loaded = bool((snapshot.get("checkpoint", {}) or {}).get("context_loaded", False))
        if needs_context_read and not context_loaded:
            if not _has_pending(snapshot, "READ_REPO_FILE") and not _has_pending(snapshot, "READ_REF"):
                request_ref = str(spec.get("request_text_ref", "")).strip()
                actions.append(_plan_read_context_action(snapshot, use_ref=bool(request_ref)))
            return actions
        if not _has_pending(snapshot, "EXECUTE_CMD"):
            actions.append(_plan_exec_action(snapshot))
        return actions

    if state == "EXEC_DONE" and bool(spec.get("review_requested", True)):
        if not _has_pending(snapshot, "REQUEST_REVIEW"):
            actions.append(_plan_review_action(snapshot))
        return actions

    return actions
