#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from action_protocol import validate_action_payload
from acceptance_spec import ensure_acceptance_spec
from coordinator_backend import BackendProtocolViolation, resolve_backend
from delivery_gate import evaluate_delivery_gate
from event_sourcing import (
    STATES,
    append_event,
    cancel_pending_actions,
    enqueue_action,
    enqueue_control_command,
    heartbeat_tick,
    list_pending_actions,
    list_pending_control_commands,
    load_checkpoint,
    load_control_payload,
    load_events,
    move_control_file,
    recent_event_digests,
    register_repo_ref,
    transition_state,
    watchdog_scan,
    write_checkpoint,
    write_state_snapshot,
)
from agn_api.ssot_store import SSOTStore
from agn_api.task_engine import validate_daily_research_contract
from research_flow import drive_research_task
from recovery_policy import decide_recovery
from state_snapshot import build_ref_index, build_state_snapshot
try:
    from agn_refs import build_repo_ref
except ImportError:  # pragma: no cover - package import fallback
    from scripts.agn_refs import build_repo_ref

try:
    from pointer_protocol import write_json_artifact, write_text_artifact
except ImportError:  # pragma: no cover - package import fallback
    from scripts.pointer_protocol import write_json_artifact, write_text_artifact

DEFAULT_BUDGET = {"max_time_sec": 900, "max_disk_mb": 512, "max_log_kb": 512}
REQUEST_INLINE_LIMIT = max(512, int((__import__("os").environ.get("AGN_DISPATCH_REQUEST_INLINE_LIMIT", "4096") or "4096")))


def _trace_id_for_task(task: dict[str, Any]) -> str:
    task_id = str(task.get("id", "task")).strip().replace("/", "_")
    correlation = str(task.get("correlation_id", "")).strip()
    if correlation:
        safe = correlation.replace("/", "_")
        # Prevent multi-task contamination when the same correlation_id is reused.
        prior = load_events(safe)
        if prior:
            seen_task_ids = {str(event.get("task_id", "")).strip() for event in prior if isinstance(event, dict)}
            seen_task_ids.discard("")
            if seen_task_ids and (seen_task_ids - {task_id}):
                return f"{safe}__{task_id}"
        return safe
    return f"trace-{task_id}"


def _latest_action_result(events: list[dict[str, Any]], action_id: str) -> dict[str, Any] | None:
    target = str(action_id).strip()
    if not target:
        return None
    for event in reversed(events):
        if str(event.get("event_type", "")).strip() != "ACTION_FINISHED":
            continue
        if str(event.get("action_id", "")).strip() != target:
            continue
        payload = event.get("payload")
        if isinstance(payload, dict):
            return payload
    return None


def _has_action_started(events: list[dict[str, Any]], action_id: str) -> bool:
    target = str(action_id).strip()
    if not target:
        return False
    for event in reversed(events):
        if str(event.get("event_type", "")).strip() != "ACTION_STARTED":
            continue
        if str(event.get("action_id", "")).strip() == target:
            return True
    return False


def _load_pending_action_payloads(*, task_id: str, trace_id: str) -> list[dict[str, Any]]:
    pending: list[dict[str, Any]] = []
    for path in list_pending_actions(task_id=task_id, trace_id=trace_id):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            pending.append(payload)
    return pending


def _ensure_task_attempt(task: dict[str, Any]) -> int:
    attempt = int(task.get("attempt", 1) or 1)
    if attempt < 1:
        attempt = 1
    task["attempt"] = attempt
    return attempt


def _apply_checkpoint_updates(task_id: str, checkpoint: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(updates, dict) or not updates:
        return checkpoint
    merged = dict(checkpoint)
    for key, value in updates.items():
        merged[key] = value
    write_checkpoint(task_id, merged)
    return load_checkpoint(task_id) or merged


def _emit_need_admin_summary(*, task: dict[str, Any], trace_id: str, reason: str) -> str:
    task_id = str(task.get("id", "")).strip()
    attempt = _ensure_task_attempt(task)
    content = (
        f"state=NEED_ADMIN\n"
        f"task_id={task_id}\n"
        f"trace_id={trace_id}\n"
        f"reason={reason}\n"
        f"acceptance_spec_ref={str(task.get('acceptance_spec_ref', '')).strip()}\n"
        f"request_text_ref={str(task.get('request_text_ref', '')).strip()}\n"
        f"task_spec_ref={str(task.get('task_spec_ref', '')).strip()}\n"
    )
    artifact = write_text_artifact(
        task_id=task_id,
        attempt=attempt,
        artifact_id="need_admin_summary",
        content=content,
        media_type="text/plain",
        filename="need_admin_summary.txt",
        source="coordinator_heartbeat",
    )
    append_event(
        trace_id=trace_id,
        task_id=task_id,
        event_type="NEED_ADMIN_SUMMARY",
        payload={"reason": reason, "summary_ref": artifact.ref},
        severity="warn",
    )
    return artifact.ref


def _compact_task_spec_payload(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_summary": str(task.get("request_summary", "")).strip(),
        "request_text_ref": str(task.get("request_text_ref", "")).strip(),
        "acceptance_criteria": task.get("acceptance_criteria", []),
        "repo_id": str(task.get("repo_id", "main")).strip() or "main",
        "repo_ref": str(task.get("repo_ref", "")).strip(),
        "work_branch": str(task.get("work_branch", "")).strip(),
        "needs_context_read": bool(task.get("needs_context_read", False)),
        "context_read_path": str(task.get("context_read_path", "README.md")).strip() or "README.md",
    }


def _apply_modify_control(
    *,
    store: SSOTStore,
    task: dict[str, Any],
    trace_id: str,
    checkpoint: dict[str, Any],
    control: dict[str, Any],
) -> tuple[bool, str, dict[str, Any]]:
    task_id = str(task.get("id", "")).strip()
    payload = control.get("payload")
    if not isinstance(payload, dict):
        return False, "invalid_modify_payload", {}
    state = str(checkpoint.get("state", "CREATED")).strip().upper() or "CREATED"
    if state in {"DELIVERED", "ABORTED", "NEED_ADMIN"}:
        return False, f"modify_not_allowed_in_terminal_state:{state}", {}

    attempt = _ensure_task_attempt(task)
    request_text = str(payload.get("request_text", "")).strip()
    request_summary = str(payload.get("request_summary", "")).strip()
    request_ref = str(payload.get("request_text_ref", "")).strip()

    if request_text:
        if len(request_text) > REQUEST_INLINE_LIMIT:
            artifact = write_text_artifact(
                task_id=task_id,
                attempt=attempt,
                artifact_id="modify_request_text",
                content=request_text,
                media_type="text/plain",
                filename="modify_request_text.txt",
                source="coordinator_heartbeat",
            )
            request_ref = artifact.ref
            if not request_summary:
                request_summary = request_text[:456] + "...<truncated-summary>..."
            task["request_text"] = ""
        else:
            # Keep short updates compact but still provide pointer for replay.
            artifact = write_text_artifact(
                task_id=task_id,
                attempt=attempt,
                artifact_id="modify_request_text_inline",
                content=request_text,
                media_type="text/plain",
                filename="modify_request_text_inline.txt",
                source="coordinator_heartbeat",
            )
            request_ref = artifact.ref
            if not request_summary:
                request_summary = request_text
            task["request_text"] = request_text

    if request_ref:
        task["request_text_ref"] = request_ref
    if request_summary:
        task["request_summary"] = request_summary[:480]

    criteria = payload.get("acceptance_criteria")
    if isinstance(criteria, list) and criteria:
        task["acceptance_criteria"] = criteria

    if "needs_context_read" in payload:
        task["needs_context_read"] = bool(payload.get("needs_context_read"))
    if "context_read_path" in payload:
        task["context_read_path"] = str(payload.get("context_read_path") or "README.md").strip() or "README.md"

    spec_ref = write_json_artifact(
        task_id=task_id,
        attempt=attempt,
        artifact_id="task_spec",
        payload=_compact_task_spec_payload(task),
        filename="task_spec.json",
        source="coordinator_heartbeat",
    )
    task["task_spec_ref"] = spec_ref.ref
    store.save_task(task)

    cancelled = cancel_pending_actions(trace_id=trace_id, task_id=task_id, reason="control_modify")
    checkpoint = load_checkpoint(task_id) or checkpoint
    checkpoint["context_loaded"] = False
    checkpoint["read_context_action_id"] = ""
    checkpoint["exec_action_id"] = ""
    checkpoint["review_action_id"] = ""
    checkpoint["spec_revision"] = int(checkpoint.get("spec_revision", 0) or 0) + 1
    checkpoint["last_control_type"] = "MODIFY"
    checkpoint["last_event_time"] = checkpoint.get("last_event_time", "")
    write_checkpoint(task_id, checkpoint)

    state = str(checkpoint.get("state", "CREATED")).strip().upper() or "CREATED"
    if state not in {"PLANNED", "ABORTED", "DELIVERED"}:
        transition_state(trace_id=trace_id, task_id=task_id, to_state="PLANNED", reason="control modify preemption")

    metadata = {
        "task_spec_ref": spec_ref.ref,
        "cancelled_actions": len(cancelled),
        "spec_revision": checkpoint["spec_revision"],
    }
    return True, "", metadata


def _apply_control_for_task(
    *,
    store: SSOTStore,
    task: dict[str, Any],
    trace_id: str,
    checkpoint: dict[str, Any],
) -> list[dict[str, Any]]:
    task_id = str(task.get("id", "")).strip()
    applied: list[dict[str, Any]] = []
    for path in list_pending_control_commands(task_id=task_id):
        control = load_control_payload(path)
        ctype = str(control.get("control_type", "")).strip().upper()
        control_id = str(control.get("control_id", path.stem)).strip()
        if not ctype:
            move_control_file(path, status="failed")
            continue

        checkpoint = load_checkpoint(task_id) or checkpoint
        try:
            if ctype == "PAUSE":
                checkpoint["paused"] = True
                checkpoint["last_control_type"] = "PAUSE"
                write_checkpoint(task_id, checkpoint)
                append_event(
                    trace_id=trace_id,
                    task_id=task_id,
                    event_type="CONTROL_APPLIED",
                    payload={"control_type": ctype, "control_id": control_id, "paused": True},
                    severity="warn",
                )
            elif ctype == "RESUME":
                checkpoint["paused"] = False
                checkpoint["last_control_type"] = "RESUME"
                write_checkpoint(task_id, checkpoint)
                append_event(
                    trace_id=trace_id,
                    task_id=task_id,
                    event_type="CONTROL_APPLIED",
                    payload={"control_type": ctype, "control_id": control_id, "paused": False},
                )
            elif ctype == "STOP":
                cancelled = cancel_pending_actions(trace_id=trace_id, task_id=task_id, reason="control_stop")
                checkpoint["paused"] = False
                write_checkpoint(task_id, checkpoint)
                transition_state(trace_id=trace_id, task_id=task_id, to_state="ABORTED", reason="control stop")
                append_event(
                    trace_id=trace_id,
                    task_id=task_id,
                    event_type="CONTROL_APPLIED",
                    payload={"control_type": ctype, "control_id": control_id, "cancelled_actions": len(cancelled)},
                    severity="warn",
                )
            elif ctype == "STATUS":
                latest = load_checkpoint(task_id) or {}
                append_event(
                    trace_id=trace_id,
                    task_id=task_id,
                    event_type="CONTROL_STATUS_SNAPSHOT",
                    payload={
                        "control_id": control_id,
                        "state": str(latest.get("state", "CREATED")),
                        "paused": bool(latest.get("paused", False)),
                        "research_phase": str(latest.get("research_phase", "")).strip(),
                        "round": int(latest.get("round", 0) or 0),
                    },
                )
            elif ctype == "DEGRADE":
                checkpoint["force_degrade"] = True
                checkpoint["last_control_type"] = "DEGRADE"
                write_checkpoint(task_id, checkpoint)
                append_event(
                    trace_id=trace_id,
                    task_id=task_id,
                    event_type="CONTROL_APPLIED",
                    payload={"control_type": ctype, "control_id": control_id, "force_degrade": True},
                    severity="warn",
                )
            elif ctype == "REORGANIZE":
                checkpoint["force_reorganize"] = True
                checkpoint["last_control_type"] = "REORGANIZE"
                write_checkpoint(task_id, checkpoint)
                append_event(
                    trace_id=trace_id,
                    task_id=task_id,
                    event_type="CONTROL_APPLIED",
                    payload={"control_type": ctype, "control_id": control_id, "force_reorganize": True},
                    severity="warn",
                )
            elif ctype == "MARK_ANOMALY":
                checkpoint["anomaly"] = True
                checkpoint["force_anomaly"] = True
                checkpoint["last_control_type"] = "MARK_ANOMALY"
                write_checkpoint(task_id, checkpoint)
                append_event(
                    trace_id=trace_id,
                    task_id=task_id,
                    event_type="CONTROL_APPLIED",
                    payload={"control_type": ctype, "control_id": control_id, "anomaly": True},
                    severity="warn",
                )
            elif ctype == "FALLBACK_TOPIC":
                payload = control.get("payload")
                if not isinstance(payload, dict):
                    payload = {}
                fallback_topic_id = str(payload.get("fallback_topic_id", "")).strip()
                checkpoint["forced_fallback_topic_id"] = fallback_topic_id
                checkpoint["last_control_type"] = "FALLBACK_TOPIC"
                write_checkpoint(task_id, checkpoint)
                append_event(
                    trace_id=trace_id,
                    task_id=task_id,
                    event_type="CONTROL_APPLIED",
                    payload={
                        "control_type": ctype,
                        "control_id": control_id,
                        "fallback_topic_id": fallback_topic_id,
                    },
                )
            elif ctype == "MODIFY":
                ok, err, metadata = _apply_modify_control(
                    store=store,
                    task=task,
                    trace_id=trace_id,
                    checkpoint=checkpoint,
                    control=control,
                )
                if not ok:
                    append_event(
                        trace_id=trace_id,
                        task_id=task_id,
                        event_type="CONTROL_REJECTED",
                        payload={"control_type": ctype, "control_id": control_id, "reason": err},
                        severity="error",
                    )
                    move_control_file(path, status="failed")
                    continue
                append_event(
                    trace_id=trace_id,
                    task_id=task_id,
                    event_type="CONTROL_APPLIED",
                    payload={"control_type": ctype, "control_id": control_id, **metadata},
                )
            else:
                append_event(
                    trace_id=trace_id,
                    task_id=task_id,
                    event_type="CONTROL_REJECTED",
                    payload={"control_type": ctype, "control_id": control_id, "reason": "unknown_control_type"},
                    severity="error",
                )
                move_control_file(path, status="failed")
                continue
        except Exception as exc:
            append_event(
                trace_id=trace_id,
                task_id=task_id,
                event_type="CONTROL_REJECTED",
                payload={"control_type": ctype, "control_id": control_id, "reason": f"{type(exc).__name__}:{exc}"},
                severity="error",
            )
            move_control_file(path, status="failed")
            continue

        move_control_file(path, status="done")
        applied.append(control)
    return applied


def _advance_state_from_events(*, task: dict[str, Any], trace_id: str, task_id: str) -> dict[str, Any]:
    checkpoint = load_checkpoint(task_id) or {
        "task_id": task_id,
        "trace_id": trace_id,
        "state": "CREATED",
        "last_event_time": "",
    }
    state = str(checkpoint.get("state", "CREATED")).strip().upper() or "CREATED"
    if state not in STATES:
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="PROTOCOL_VIOLATION",
            payload={"reason": f"invalid_checkpoint_state:{state}"},
            severity="error",
        )
        checkpoint["state"] = "PLANNED"
        checkpoint["state_reason"] = "recovered invalid checkpoint state"
        write_checkpoint(task_id, checkpoint)
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="STATE_RECOVERED",
            payload={"from": state, "to": "PLANNED", "reason": "invalid checkpoint state"},
            severity="warn",
        )
        checkpoint = load_checkpoint(task_id) or checkpoint
        state = str(checkpoint.get("state", "PLANNED")).strip().upper() or "PLANNED"
    events = load_events(trace_id)

    if state == "CREATED":
        transition_state(trace_id=trace_id, task_id=task_id, to_state="PLANNED", reason="heartbeat first plan")
        checkpoint = load_checkpoint(task_id) or checkpoint
        state = str(checkpoint.get("state", "PLANNED")).strip().upper() or "PLANNED"

    # Read-by-action completion does not change global state, but unlocks execution planning.
    read_action_id = str(checkpoint.get("read_context_action_id", "")).strip()
    if read_action_id:
        read_result = _latest_action_result(events, read_action_id)
        if read_result is not None:
            rc = int(read_result.get("rc", 0) or 0)
            checkpoint["context_loaded"] = rc == 0
            checkpoint["read_context_action_id"] = ""
            write_checkpoint(task_id, checkpoint)
            append_event(
                trace_id=trace_id,
                task_id=task_id,
                event_type="CONTEXT_READ_READY" if rc == 0 else "CONTEXT_READ_FAILED",
                action_id=read_action_id,
                payload={"rc": rc},
                severity="info" if rc == 0 else "warn",
            )
            checkpoint = load_checkpoint(task_id) or checkpoint
            state = str(checkpoint.get("state", state)).strip().upper() or state

    if state == "DISPATCHED_EXEC":
        exec_action_id = str(checkpoint.get("exec_action_id", "")).strip()
        if exec_action_id and _has_action_started(events, exec_action_id):
            transition_state(trace_id=trace_id, task_id=task_id, to_state="EXEC_RUNNING", reason="runner started exec action")

    checkpoint = load_checkpoint(task_id) or checkpoint
    state = str(checkpoint.get("state", state)).strip().upper() or state
    if state == "EXEC_RUNNING":
        exec_action_id = str(checkpoint.get("exec_action_id", "")).strip()
        result = _latest_action_result(events, exec_action_id) if exec_action_id else None
        if result is not None:
            rc = int(result.get("rc", 0) or 0)
            if rc == 0:
                transition_state(trace_id=trace_id, task_id=task_id, to_state="EXEC_DONE", reason="exec completed")
            else:
                append_event(
                    trace_id=trace_id,
                    task_id=task_id,
                    event_type="EXEC_FAILURE_DETECTED",
                    action_id=exec_action_id,
                    payload={"rc": rc, "reason": "defer to recovery policy"},
                    severity="warn",
                )
                transition_state(trace_id=trace_id, task_id=task_id, to_state="PLANNED", reason=f"exec failed rc={rc}; recovery pending")

    checkpoint = load_checkpoint(task_id) or checkpoint
    state = str(checkpoint.get("state", state)).strip().upper() or state
    if state == "EXEC_DONE" and not bool(task.get("review_requested", True)):
        transition_state(trace_id=trace_id, task_id=task_id, to_state="SYNTHESIS", reason="review skipped")

    checkpoint = load_checkpoint(task_id) or checkpoint
    state = str(checkpoint.get("state", state)).strip().upper() or state
    if state == "DISPATCHED_REVIEW":
        review_action_id = str(checkpoint.get("review_action_id", "")).strip()
        if review_action_id and _has_action_started(events, review_action_id):
            transition_state(trace_id=trace_id, task_id=task_id, to_state="REVIEW_RUNNING", reason="runner started review")

    checkpoint = load_checkpoint(task_id) or checkpoint
    state = str(checkpoint.get("state", state)).strip().upper() or state
    if state == "REVIEW_RUNNING":
        review_action_id = str(checkpoint.get("review_action_id", "")).strip()
        result = _latest_action_result(events, review_action_id) if review_action_id else None
        if result is not None:
            rc = int(result.get("rc", 0) or 0)
            if rc == 0:
                transition_state(trace_id=trace_id, task_id=task_id, to_state="REVIEW_DONE", reason="review completed")
            else:
                transition_state(trace_id=trace_id, task_id=task_id, to_state="ABORTED", reason=f"review failed rc={rc}")

    checkpoint = load_checkpoint(task_id) or checkpoint
    state = str(checkpoint.get("state", state)).strip().upper() or state
    if state == "REVIEW_DONE":
        transition_state(trace_id=trace_id, task_id=task_id, to_state="SYNTHESIS", reason="ready for synthesis")

    checkpoint = load_checkpoint(task_id) or checkpoint
    state = str(checkpoint.get("state", state)).strip().upper() or state
    if state == "SYNTHESIS":
        transition_state(trace_id=trace_id, task_id=task_id, to_state="DELIVERY_GATE", reason="synthesis completed")

    return load_checkpoint(task_id) or checkpoint


def _enqueue_backend_actions(*, actions: list[dict[str, Any]], trace_id: str, task_id: str) -> list[dict[str, Any]]:
    checkpoint = load_checkpoint(task_id) or {}
    state = str(checkpoint.get("state", "CREATED")).strip().upper() or "CREATED"
    planned: list[dict[str, Any]] = []

    for action in actions:
        vr = validate_action_payload(action)
        if not vr.valid:
            append_event(
                trace_id=trace_id,
                task_id=task_id,
                event_type="PROTOCOL_VIOLATION",
                action_id=str(action.get("action_id", "")).strip(),
                payload={"errors": vr.errors, "source": "coordinator_backend"},
                severity="error",
            )
            continue

        enqueue_action(action)
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="ACTION_PLANNED",
            action_id=str(action.get("action_id", "")).strip(),
            payload={"action_type": str(action.get("action_type", "")).strip()},
        )
        planned.append(action)

        action_type = str(action.get("action_type", "")).strip()
        action_id = str(action.get("action_id", "")).strip()
        checkpoint = load_checkpoint(task_id) or checkpoint
        state = str(checkpoint.get("state", state)).strip().upper() or state
        if action_type == "EXECUTE_CMD" and state == "PLANNED":
            transition_state(trace_id=trace_id, task_id=task_id, to_state="DISPATCHED_EXEC", reason="exec action enqueued")
            checkpoint = load_checkpoint(task_id) or checkpoint
            checkpoint["exec_action_id"] = action_id
            write_checkpoint(task_id, checkpoint)
        elif action_type == "REQUEST_REVIEW" and state in {"EXEC_DONE", "PLANNED", "DISPATCHED_REVIEW"}:
            if state in {"EXEC_DONE", "PLANNED"}:
                transition_state(trace_id=trace_id, task_id=task_id, to_state="DISPATCHED_REVIEW", reason="review action enqueued")
                checkpoint = load_checkpoint(task_id) or checkpoint
            checkpoint["review_action_id"] = action_id
            write_checkpoint(task_id, checkpoint)
        elif action_type in {"READ_REF", "READ_REPO_FILE"}:
            checkpoint["read_context_action_id"] = action_id
            write_checkpoint(task_id, checkpoint)
        elif action_type == "ABORT":
            transition_state(trace_id=trace_id, task_id=task_id, to_state="ABORTED", reason="abort action planned")

    return planned


def _drive_task(*, store: SSOTStore, task: dict[str, Any], backend_name: str) -> dict[str, Any]:
    task_id = str(task.get("id", "")).strip()
    trace_id = _trace_id_for_task(task)
    if not task_id:
        return {"task_id": "", "trace_id": trace_id, "status": "skipped"}

    attempt = _ensure_task_attempt(task)
    repo_id = str(task.get("repo_id", "main")).strip() or "main"
    repo_ref = str(task.get("repo_ref", "")).strip() or build_repo_ref(repo_id)
    task["repo_id"] = repo_id
    task["repo_ref"] = repo_ref
    repo_path = str(task.get("repo_path", "")).strip()
    if repo_path:
        register_repo_ref(repo_ref=repo_ref, repo_path=repo_path)

    task, spec_ref, spec_created, spec_errors = ensure_acceptance_spec(
        store=store,
        task=task,
        trace_id=trace_id,
        attempt=attempt,
    )
    if spec_errors:
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="PROTOCOL_VIOLATION",
            payload={"source": "acceptance_spec", "errors": spec_errors},
            severity="error",
        )
    elif spec_created:
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="ACCEPTANCE_SPEC_CREATED",
            payload={"acceptance_spec_ref": spec_ref},
        )
    store.save_task(task)

    heartbeat_tick(trace_id=trace_id, task_id=task_id, note="coordinator heartbeat")

    checkpoint = load_checkpoint(task_id) or {
        "task_id": task_id,
        "trace_id": trace_id,
        "state": "CREATED",
        "last_event_time": "",
        "paused": False,
    }
    write_checkpoint(task_id, checkpoint)

    control_applied = _apply_control_for_task(store=store, task=task, trace_id=trace_id, checkpoint=checkpoint)
    checkpoint = load_checkpoint(task_id) or checkpoint

    research_errors = validate_daily_research_contract(task)
    if research_errors:
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="PROTOCOL_VIOLATION",
            payload={"source": "daily_research_contract", "errors": research_errors},
            severity="error",
        )
        if str(task.get("task_kind", "")).strip() == "daily_research":
            blocked_checkpoint = dict(load_checkpoint(task_id) or checkpoint)
            blocked_checkpoint["task_id"] = task_id
            blocked_checkpoint["trace_id"] = trace_id
            blocked_checkpoint["research_phase"] = str(
                blocked_checkpoint.get(
                    "research_phase",
                    "manual_intake" if str(task.get("research_trigger_mode", "")).strip().lower() == "manual" else "auto_survey",
                )
            ).strip() or ("manual_intake" if str(task.get("research_trigger_mode", "")).strip().lower() == "manual" else "auto_survey")
            blocked_checkpoint["protocol_blocked"] = True
            blocked_checkpoint["protocol_block_reason"] = "daily_research_contract_incomplete"
            blocked_checkpoint["research_status"] = "protocol_blocked"
            blocked_checkpoint["governance_ready"] = False
            blocked_checkpoint["governance_missing"] = list(research_errors)
            blocked_checkpoint["last_event_time"] = blocked_checkpoint.get("last_event_time", "")
            write_checkpoint(task_id, blocked_checkpoint)
            return {
                "task_id": task_id,
                "trace_id": trace_id,
                "state": str(blocked_checkpoint.get("state", checkpoint.get("state", ""))).strip(),
                "paused": bool(blocked_checkpoint.get("paused", False)),
                "controls_applied": len(control_applied),
                "actions_planned": 0,
                "snapshot_path": "",
                "backend": "research_main_chain_blocked",
                "research_phase": str(blocked_checkpoint.get("research_phase", "")).strip(),
                "research_status": "protocol_blocked",
                "protocol_blocked": True,
                "protocol_block_reason": "daily_research_contract_incomplete",
                "governance_missing": list(research_errors),
            }

    if str(task.get("task_kind", "")).strip() == "daily_research":
        research_summary = drive_research_task(store=store, task=task)
        final_checkpoint = load_checkpoint(task_id) or checkpoint
        return {
            **research_summary,
            "task_id": task_id,
            "trace_id": str(research_summary.get("trace_id", trace_id)).strip() or trace_id,
            "state": str(research_summary.get("state", final_checkpoint.get("state", ""))).strip(),
            "paused": bool(final_checkpoint.get("paused", False)),
            "controls_applied": len(control_applied),
            "actions_planned": 0,
            "snapshot_path": "",
            "backend": "research_main_chain",
        }

    checkpoint = _advance_state_from_events(task=task, trace_id=trace_id, task_id=task_id)
    checkpoint = load_checkpoint(task_id) or checkpoint

    events = load_events(trace_id)
    pending_actions = _load_pending_action_payloads(task_id=task_id, trace_id=trace_id)
    gate_actions: list[dict[str, Any]] = []
    recovery_actions: list[dict[str, Any]] = []

    state = str(checkpoint.get("state", "CREATED")).strip().upper() or "CREATED"
    if state == "DELIVERY_GATE":
        decision = evaluate_delivery_gate(
            trace_id=trace_id,
            task=task,
            checkpoint=checkpoint,
            events=events,
            pending_actions=pending_actions,
        )
        if decision.updated_spec_ref:
            task["acceptance_spec_ref"] = decision.updated_spec_ref
            store.save_task(task)
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="DELIVERY_GATE_EVALUATED",
            payload={
                "status": decision.status,
                "reason": decision.reason,
                "missing_required": decision.missing_required,
                "unresolved_refs": decision.unresolved_refs,
                "validator_failures": decision.validator_failures,
                "acceptance_spec_ref": str(task.get("acceptance_spec_ref", "")).strip(),
            },
            severity="info" if decision.status == "PASS" else "warn",
        )

        checkpoint = load_checkpoint(task_id) or checkpoint
        if decision.status == "PASS":
            checkpoint["gate_fail_streak"] = 0
            write_checkpoint(task_id, checkpoint)
            transition_state(trace_id=trace_id, task_id=task_id, to_state="DELIVERED", reason="delivery gate passed")
        else:
            checkpoint["gate_fail_streak"] = int(checkpoint.get("gate_fail_streak", 0) or 0) + 1
            write_checkpoint(task_id, checkpoint)
            append_event(
                trace_id=trace_id,
                task_id=task_id,
                event_type="DELIVERY_GATE_FAILED",
                payload={
                    "reason": decision.reason,
                    "gate_fail_streak": checkpoint["gate_fail_streak"],
                    "missing_required": decision.missing_required,
                    "unresolved_refs": decision.unresolved_refs,
                    "validator_failures": decision.validator_failures,
                },
                severity="warn",
            )
            if decision.actions:
                action_types = {
                    str(action.get("action_type", "")).strip()
                    for action in decision.actions
                    if isinstance(action, dict)
                }
                if action_types == {"REQUEST_REVIEW"} and bool(task.get("review_requested", False)):
                    transition_state(trace_id=trace_id, task_id=task_id, to_state="DISPATCHED_REVIEW", reason="delivery gate review-only loopback")
                else:
                    transition_state(trace_id=trace_id, task_id=task_id, to_state="PLANNED", reason="delivery gate loopback")
                gate_actions = decision.actions

    checkpoint = load_checkpoint(task_id) or checkpoint
    recovery = decide_recovery(trace_id=trace_id, task=task, checkpoint=checkpoint, events=events)
    checkpoint = _apply_checkpoint_updates(task_id, checkpoint, recovery.checkpoint_updates)
    if recovery.escalate:
        transition_state(trace_id=trace_id, task_id=task_id, to_state="NEED_ADMIN", reason=recovery.escalate_reason)
        checkpoint = load_checkpoint(task_id) or checkpoint
        checkpoint["paused"] = True
        write_checkpoint(task_id, checkpoint)
        gate_actions = []
        recovery_actions = []
        summary_ref = _emit_need_admin_summary(task=task, trace_id=trace_id, reason=recovery.escalate_reason)
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="ESCALATION_TRIGGERED",
            payload={"reason": recovery.escalate_reason, "summary_ref": summary_ref},
            severity="warn",
        )
    else:
        recovery_actions = recovery.actions

    # Stage 2 input: agn_event_queue digest
    digests = recent_event_digests(trace_id=trace_id, limit=30)
    checkpoint = load_checkpoint(task_id) or checkpoint
    perf_budget = task.get("perf_budget") if isinstance(task.get("perf_budget"), dict) else dict(DEFAULT_BUDGET)
    ref_index = build_ref_index(task=task, checkpoint=checkpoint, recent_event_digests=digests, limit=64)
    snapshot, snapshot_ref = build_state_snapshot(
        trace_id=trace_id,
        task_id=task_id,
        attempt=attempt,
        task=task,
        checkpoint=checkpoint,
        pending_actions=pending_actions,
        recent_event_digests=digests,
        ref_index=ref_index,
        perf_limits=perf_budget,
    )
    snapshot_path = write_state_snapshot(trace_id=trace_id, task_id=task_id, snapshot=snapshot, snapshot_ref=snapshot_ref)

    backend = resolve_backend(backend_name)
    state_after = str((load_checkpoint(task_id) or checkpoint).get("state", "CREATED")).strip().upper() or "CREATED"
    proposed: list[dict[str, Any]] = []
    if not gate_actions and not recovery_actions and state_after not in {"DELIVERED", "ABORTED", "NEED_ADMIN", "DELIVERY_GATE"}:
        try:
            proposed = backend.propose_actions(
                snapshot=snapshot,
                recent_event_digests=digests,
                control_commands=control_applied,
                ref_index=ref_index,
            )
        except BackendProtocolViolation as exc:
            append_event(
                trace_id=trace_id,
                task_id=task_id,
                event_type="PROTOCOL_VIOLATION",
                payload={"source": f"backend:{backend.name}", "errors": exc.errors},
                severity="error",
            )
            proposed = []
        except Exception as exc:  # pragma: no cover - defensive path
            append_event(
                trace_id=trace_id,
                task_id=task_id,
                event_type="PROTOCOL_VIOLATION",
                payload={"source": f"backend:{backend.name}", "errors": [f"{type(exc).__name__}:{exc}"]},
                severity="error",
            )
            proposed = []

    planned = _enqueue_backend_actions(actions=[*gate_actions, *recovery_actions, *proposed], trace_id=trace_id, task_id=task_id)

    final_checkpoint = load_checkpoint(task_id) or checkpoint
    try:
        snapshot_rel = str(snapshot_path.resolve().relative_to(ROOT))
    except Exception:
        snapshot_rel = snapshot_path.name
    return {
        "task_id": task_id,
        "trace_id": trace_id,
        "state": str(final_checkpoint.get("state", "")).strip(),
        "paused": bool(final_checkpoint.get("paused", False)),
        "controls_applied": len(control_applied),
        "actions_planned": len(planned),
        "snapshot_path": snapshot_rel,
        "backend": backend.name,
    }


def _fanout_broadcast_controls(tasks: list[dict[str, Any]]) -> int:
    expanded = 0
    if not tasks:
        return expanded
    task_ids = [str(task.get("id", "")).strip() for task in tasks if isinstance(task, dict) and str(task.get("id", "")).strip()]
    if not task_ids:
        return expanded
    for path in list_pending_control_commands():
        control = load_control_payload(path)
        if not isinstance(control, dict):
            continue
        ctype = str(control.get("control_type", "")).strip().upper()
        if not ctype:
            continue
        bound_task = str(control.get("task_id", "")).strip()
        if bound_task:
            continue
        control_id = str(control.get("control_id", path.stem)).strip()
        payload = control.get("payload", {})
        for task_id in task_ids:
            enqueue_control_command(
                {
                    "control_type": ctype,
                    "control_id": control_id,
                    "task_id": task_id,
                    "payload": payload if isinstance(payload, dict) else {},
                }
            )
            expanded += 1
        move_control_file(path, status="done")
    return expanded


def run_tick(
    *,
    max_tasks: int = 20,
    timeout_sec: int = 300,
    task_filter: str | None = None,
    backend_name: str = "local",
) -> dict[str, Any]:
    store = SSOTStore(ROOT / "ssot")
    tasks = [
        task
        for task in store.list_tasks()
        if isinstance(task, dict)
        and task.get("agn_managed")
        and (not task_filter or str(task.get("id", "")).strip() == str(task_filter).strip())
    ]
    broadcast_expanded = _fanout_broadcast_controls(tasks)
    processed = 0
    summaries: list[dict[str, Any]] = []
    controls_applied = 0

    for task in tasks:
        if processed >= max_tasks:
            break
        summary = _drive_task(store=store, task=task, backend_name=backend_name)
        summaries.append(summary)
        controls_applied += int(summary.get("controls_applied", 0) or 0)
        processed += 1

    watchdog = watchdog_scan(timeout_sec=timeout_sec)
    return {
        "processed": processed,
        "watchdog_triggered": len(watchdog),
        "controls_applied": controls_applied,
        "broadcast_controls_expanded": broadcast_expanded,
        "backend": resolve_backend(backend_name).name,
        "summaries": summaries,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Event-driven coordinator heartbeat loop")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-tasks", type=int, default=20)
    parser.add_argument("--timeout-sec", type=int, default=300)
    parser.add_argument("--task-id", default="")
    parser.add_argument("--backend", default="local", choices=["local", "remote_mock"])
    args = parser.parse_args()
    summary = run_tick(
        max_tasks=max(1, int(args.max_tasks)),
        timeout_sec=max(30, int(args.timeout_sec)),
        task_filter=str(args.task_id).strip() or None,
        backend_name=str(args.backend or "local").strip(),
    )
    print(json.dumps(summary, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
