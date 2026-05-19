#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

try:
    from acceptance_spec import apply_evidence_updates, load_acceptance_spec, validate_acceptance_spec
    from action_protocol import build_action
    from agn_refs import build_object_ref, parse_object_ref
    from pointer_protocol import resolve_ref_path, write_json_artifact
except ImportError:  # pragma: no cover - package import fallback
    from scripts.acceptance_spec import apply_evidence_updates, load_acceptance_spec, validate_acceptance_spec
    from scripts.action_protocol import build_action
    from scripts.agn_refs import build_object_ref, parse_object_ref
    from scripts.pointer_protocol import resolve_ref_path, write_json_artifact

ROOT = Path(__file__).resolve().parents[1]
PERF_DIR = ROOT / ".agn_workspace" / "event_driven" / "ssot" / "perf"
DEFAULT_BUDGET = {"max_time_sec": 900, "max_disk_mb": 512, "max_log_kb": 512}


@dataclass
class GateDecision:
    status: str
    reason: str
    missing_required: list[str] = field(default_factory=list)
    unresolved_refs: list[str] = field(default_factory=list)
    validator_failures: list[str] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    updated_spec_ref: str = ""



def _next_action_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def _task_attempt(task: dict[str, Any]) -> int:
    return max(1, int(task.get("attempt", 1) or 1))


def _budget(task: dict[str, Any]) -> dict[str, Any]:
    raw = task.get("perf_budget")
    if not isinstance(raw, dict):
        return dict(DEFAULT_BUDGET)
    return {
        "max_time_sec": float(raw.get("max_time_sec", DEFAULT_BUDGET["max_time_sec"]) or DEFAULT_BUDGET["max_time_sec"]),
        "max_disk_mb": float(raw.get("max_disk_mb", DEFAULT_BUDGET["max_disk_mb"]) or DEFAULT_BUDGET["max_disk_mb"]),
        "max_log_kb": float(raw.get("max_log_kb", DEFAULT_BUDGET["max_log_kb"]) or DEFAULT_BUDGET["max_log_kb"]),
    }


def _latest_success_result_ref(events: list[dict[str, Any]], *, action_type: str = "") -> str:
    expected_type = str(action_type).strip().upper()
    for event in reversed(events):
        if str(event.get("event_type", "")) != "ACTION_FINISHED":
            continue
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            continue
        finished_type = str(payload.get("action_type", "")).strip().upper()
        if expected_type and finished_type != expected_type:
            continue
        rc_raw = payload.get("rc", 1)
        try:
            rc = int(rc_raw)
        except Exception:
            rc = 1
        if rc != 0:
            continue
        result_ref = str(payload.get("result_ref", "")).strip()
        if result_ref.startswith("agn://"):
            return result_ref
    return ""


def _infer_refs_for_item(*, item: dict[str, Any], trace_id: str, task_id: str, attempt: int, events: list[dict[str, Any]]) -> list[str]:
    evidence_type = str(item.get("evidence_type", "")).strip()
    if evidence_type == "log_ref":
        candidate = _latest_success_result_ref(events, action_type="EXECUTE_CMD")
        return [candidate] if candidate else []
    if evidence_type == "result_ref":
        return [build_object_ref("result", trace_id, attempt)]
    if evidence_type == "verdict_ref":
        return [build_object_ref("verdict", trace_id, attempt)]
    if evidence_type == "metric":
        return [build_object_ref("perf", trace_id, attempt)]
    if evidence_type == "patch_ref":
        return [build_object_ref("patch", trace_id, attempt)]
    return []


def _resolve_object_ref_path(*, ref: str, task_id: str) -> Path:
    parsed = parse_object_ref(ref)
    kind = str(parsed.get("kind", "")).strip()
    trace_id = str(parsed.get("trace_id", "")).strip()
    attempt = max(1, int(parsed.get("attempt", 1) or 1))
    if kind == "dispatch":
        return (ROOT / "dispatch" / f"{task_id}.json").resolve()
    if kind == "result":
        return (ROOT / "results" / f"{task_id}.{attempt}.json").resolve()
    if kind == "verdict":
        return (ROOT / "verdicts" / f"{task_id}.{attempt}.json").resolve()
    if kind == "perf":
        return (PERF_DIR / f"{trace_id}.perf_summary.json").resolve()
    if kind == "patch":
        return (ROOT / "reports" / f"{task_id}.{attempt}.patch").resolve()
    raise ValueError(f"unsupported_object_kind:{kind}")


def _resolve_ref_text(*, ref: str, task_id: str) -> str:
    if ref.startswith("agn://artifact/"):
        path = resolve_ref_path(ref)
        return Path(path).read_text(encoding="utf-8", errors="replace")
    if ref.startswith("agn://object/"):
        path = _resolve_object_ref_path(ref=ref, task_id=task_id)
        return path.read_text(encoding="utf-8", errors="replace")
    raise ValueError("unsupported_ref_type")


def _resolve_ref_exists(*, ref: str, task_id: str) -> bool:
    try:
        if ref.startswith("agn://artifact/"):
            resolve_ref_path(ref)
            return True
        if ref.startswith("agn://object/"):
            return _resolve_object_ref_path(ref=ref, task_id=task_id).exists()
    except Exception:
        return False
    return False


def _sha256_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _validate_with_rule(*, validator: dict[str, Any], refs: list[str], task_id: str) -> str:
    if not validator:
        return ""
    kind = str(validator.get("kind", "")).strip()
    if kind == "contains":
        needle = str(validator.get("needle", "")).strip()
        if not needle:
            return ""
        try:
            text = _resolve_ref_text(ref=refs[0], task_id=task_id)
        except Exception as exc:
            return f"contains_read_failed:{type(exc).__name__}"
        return "" if needle in text else f"contains_not_found:{needle}"

    if kind == "equals":
        target = str(validator.get("value", ""))
        try:
            text = _resolve_ref_text(ref=refs[0], task_id=task_id).strip()
        except Exception as exc:
            return f"equals_read_failed:{type(exc).__name__}"
        return "" if text == target else "equals_mismatch"

    if kind == "jsonpath":
        path = str(validator.get("path", "")).strip()
        expected = validator.get("equals")
        if not path:
            return "jsonpath_missing_path"
        try:
            payload = json.loads(_resolve_ref_text(ref=refs[0], task_id=task_id))
        except Exception as exc:
            return f"jsonpath_read_failed:{type(exc).__name__}"
        cursor: Any = payload
        for token in path.split("."):
            if not token:
                continue
            if isinstance(cursor, dict) and token in cursor:
                cursor = cursor[token]
            else:
                return f"jsonpath_missing:{path}"
        if expected is not None and cursor != expected:
            return f"jsonpath_mismatch:{path}"
        return ""

    if kind == "sha256_match":
        expected = str(validator.get("sha256", "")).strip().lower()
        if len(expected) != 64:
            return "sha256_invalid_expected"
        try:
            text = _resolve_ref_text(ref=refs[0], task_id=task_id)
        except Exception as exc:
            return f"sha256_read_failed:{type(exc).__name__}"
        return "" if _sha256_text(text) == expected else "sha256_mismatch"

    return ""


def _has_pending_type(pending_actions: list[dict[str, Any]], action_type: str) -> bool:
    target = str(action_type).strip()
    for action in pending_actions:
        if not isinstance(action, dict):
            continue
        if str(action.get("action_type", "")).strip() == target:
            return True
    return False


def _loopback_actions(
    *,
    trace_id: str,
    task: dict[str, Any],
    missing_types: set[str],
    pending_actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    task_id = str(task.get("id", "")).strip()
    attempt = _task_attempt(task)
    actions: list[dict[str, Any]] = []
    budget = _budget(task)

    if ("result_ref" in missing_types or "log_ref" in missing_types or "patch_ref" in missing_types) and not _has_pending_type(
        pending_actions, "EXECUTE_CMD"
    ):
        runner_cmd = task.get("runner_cmd")
        argv = ["echo", f"re-run task {task_id}"]
        if isinstance(runner_cmd, list) and runner_cmd:
            argv = [str(x) for x in runner_cmd]
        refs: dict[str, Any] = {}
        for key in ("repo_ref", "request_text_ref", "task_spec_ref"):
            value = str(task.get(key, "")).strip()
            if value.startswith("agn://"):
                refs[key.replace("request_text_ref", "instruction_ref")] = value
        actions.append(
            build_action(
                trace_id=trace_id,
                task_id=task_id,
                action_id=_next_action_id("gateexec"),
                action_type="EXECUTE_CMD",
                inputs={"argv": argv, "attempt": attempt, "timeout_sec": 900},
                refs=refs,
                budget=budget,
                source_role="coordinator",
                state_hint="DISPATCHED_EXEC",
            )
        )

    if "verdict_ref" in missing_types and bool(task.get("review_requested", False)) and not _has_pending_type(
        pending_actions, "REQUEST_REVIEW"
    ):
        actions.append(
            build_action(
                trace_id=trace_id,
                task_id=task_id,
                action_id=_next_action_id("gatereview"),
                action_type="REQUEST_REVIEW",
                inputs={"attempt": attempt},
                refs={
                    "dispatch_ref": build_object_ref("dispatch", trace_id, attempt),
                    "result_ref": build_object_ref("result", trace_id, attempt),
                    "verdict_ref": build_object_ref("verdict", trace_id, attempt),
                },
                budget=budget,
                source_role="coordinator",
                state_hint="DISPATCHED_REVIEW",
            )
        )

    if "metric" in missing_types and not _has_pending_type(pending_actions, "SUMMARIZE"):
        actions.append(
            build_action(
                trace_id=trace_id,
                task_id=task_id,
                action_id=_next_action_id("gatemetric"),
                action_type="SUMMARIZE",
                inputs={"attempt": attempt, "content": "generate perf/metrics evidence"},
                refs={"source_refs": [build_object_ref("perf", trace_id, attempt)]},
                budget=budget,
                source_role="coordinator",
                state_hint="PLANNED",
            )
        )

    return actions


def evaluate_delivery_gate(
    *,
    trace_id: str,
    task: dict[str, Any],
    checkpoint: dict[str, Any],
    events: list[dict[str, Any]],
    pending_actions: list[dict[str, Any]],
) -> GateDecision:
    task_id = str(task.get("id", "")).strip()
    attempt = _task_attempt(task)
    spec_ref = str(task.get("acceptance_spec_ref", "")).strip()
    if not spec_ref:
        return GateDecision(status="LOOP_BACK", reason="missing_acceptance_spec", missing_required=["acceptance_spec_ref"])

    try:
        spec = load_acceptance_spec(spec_ref)
    except Exception as exc:
        return GateDecision(status="LOOP_BACK", reason=f"acceptance_spec_unreadable:{type(exc).__name__}", missing_required=["acceptance_spec_ref"])

    ok, errors = validate_acceptance_spec(spec)
    if not ok:
        return GateDecision(status="LOOP_BACK", reason="invalid_acceptance_spec", missing_required=errors)

    evidence_updates: dict[str, list[str]] = {}
    missing_required: list[str] = []
    unresolved_refs: list[str] = []
    validator_failures: list[str] = []

    items = spec.get("items", [])
    blocking = bool(spec.get("blocking", True))
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        ac_id = str(item.get("ac_id", "")).strip()
        required = bool(item.get("required", False))
        refs = [str(ref).strip() for ref in item.get("evidence_refs", []) if isinstance(ref, str)]
        if not refs:
            refs = _infer_refs_for_item(
                item=item,
                trace_id=trace_id,
                task_id=task_id,
                attempt=attempt,
                events=events,
            )
        refs = [ref for ref in refs if ref.startswith("agn://")]
        evidence_updates[ac_id] = refs

        if required and not refs:
            missing_required.append(f"{ac_id}:missing_evidence_refs")
            continue

        for ref in refs:
            if not _resolve_ref_exists(ref=ref, task_id=task_id):
                unresolved_refs.append(f"{ac_id}:{ref}")

        validator = item.get("validator")
        if isinstance(validator, dict) and refs:
            vf = _validate_with_rule(validator=validator, refs=refs, task_id=task_id)
            if vf:
                validator_failures.append(f"{ac_id}:{vf}")

    updated_spec_ref = ""
    updated_spec, changed = apply_evidence_updates(spec, evidence_updates)
    if changed:
        artifact = write_json_artifact(
            task_id=task_id,
            attempt=attempt,
            artifact_id="acceptance_spec_gate",
            payload=updated_spec,
            filename="acceptance_spec_gate.json",
            source="delivery_gate",
        )
        task["acceptance_spec_ref"] = artifact.ref
        updated_spec_ref = artifact.ref

    failed = bool(missing_required or unresolved_refs or validator_failures)
    if not failed:
        return GateDecision(status="PASS", reason="all_required_evidence_present", updated_spec_ref=updated_spec_ref)

    missing_types: set[str] = set()
    for item in updated_spec.get("items", []) if isinstance(updated_spec.get("items"), list) else []:
        if not isinstance(item, dict) or not bool(item.get("required", False)):
            continue
        refs = [str(r).strip() for r in item.get("evidence_refs", []) if isinstance(r, str)]
        ac_id = str(item.get("ac_id", "")).strip()
        if refs and not any(entry.startswith(f"{ac_id}:") for entry in unresolved_refs + validator_failures):
            continue
        missing_types.add(str(item.get("evidence_type", "")).strip())

    actions = _loopback_actions(
        trace_id=trace_id,
        task=task,
        missing_types=missing_types,
        pending_actions=pending_actions,
    )

    status = "LOOP_BACK"
    reason = "delivery_gate_failed"
    if not actions and not blocking:
        status = "PASS"
        reason = "non_blocking_items_failed"

    return GateDecision(
        status=status,
        reason=reason,
        missing_required=missing_required,
        unresolved_refs=unresolved_refs,
        validator_failures=validator_failures,
        actions=actions,
        updated_spec_ref=updated_spec_ref,
    )
