"""AGN dispatcher runtime.

This is the real package implementation for AGN's governed runtime dispatcher.
The legacy script remains only as a compatibility shim.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from agn.core.emergency_stop import dispatcher_accepts_new_work
from agn.core.policy_gate import create_gate_entry, evaluate_dispatch_request
from agn.dispatch.bus import acknowledge_message, expire_messages, publish_message
from agn.dispatch.event_store import append_event
from agn.governance.read_models import refresh_read_models
from agn.handlers.desktop import run_desktop_action
from agn.handlers.memory import append_record
from agn.handlers.providers import run_routed_task
from agn.handlers.review import run_review
from agn.handlers.vision import parse_vision_ref


PACKAGE_PATH = "agn.dispatch.dispatcher"
LEGACY_SCRIPT_SHIM = "scripts/dispatcher_runtime.py"
RUNTIME_DIR = ROOT / "runtime" / "dispatcher"
REQUESTS_DIR = RUNTIME_DIR / "requests"
RESULTS_DIR = RUNTIME_DIR / "results"
VALID_TARGET_KINDS = {
    "provider",
    "reviewer",
    "memory_recorder",
    "vision_parser",
    "desktop_adapter",
    "legacy_task",
}
VALID_RISK_LEVELS = {"low", "medium", "high"}


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def ensure_dispatcher_dirs() -> None:
    REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value or default)
    except (ValueError, TypeError):
        return default


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value or default)
    except (ValueError, TypeError):
        return default


def _normalize_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def validate_dispatcher_request(raw: dict[str, Any]) -> list[str]:
    if not isinstance(raw, dict):
        return ["request_must_be_object"]
    errors: list[str] = []
    for key in ("trace_id", "caller", "target", "target_kind", "intent", "reason"):
        if not str(raw.get(key, "")).strip():
            errors.append(f"missing:{key}")
    target_kind = str(raw.get("target_kind", "")).strip()
    if target_kind and target_kind not in VALID_TARGET_KINDS:
        errors.append("invalid:target_kind")
    risk_level = str(raw.get("risk_level", "medium")).strip().lower() or "medium"
    if risk_level not in VALID_RISK_LEVELS:
        errors.append("invalid:risk_level")
    if not isinstance(raw.get("escalation_policy", {}), dict):
        errors.append("invalid:escalation_policy")
    if not isinstance(raw.get("approval_context", {}), dict):
        errors.append("invalid:approval_context")
    return errors


def normalize_dispatcher_request(raw: dict[str, Any]) -> dict[str, Any]:
    request_id = str(raw.get("request_id", "")).strip() or f"dispatch-{uuid4().hex[:12]}"
    output_dir = str(raw.get("output_dir", "")).strip()
    if not output_dir:
        output_dir = str(RUNTIME_DIR / "outputs" / request_id)
    risk_level = str(raw.get("risk_level", "medium")).strip().lower() or "medium"
    if risk_level not in VALID_RISK_LEVELS:
        risk_level = "medium"
    trace_id = str(raw.get("trace_id", "")).strip()
    task_id = str(raw.get("task_id", "")).strip() or request_id
    return {
        "request_id": request_id,
        "trace_id": trace_id,
        "task_id": task_id,
        "caller": str(raw.get("caller", "")).strip(),
        "target": str(raw.get("target", "")).strip(),
        "target_kind": str(raw.get("target_kind", "")).strip(),
        "intent": str(raw.get("intent", "")).strip(),
        "reason": str(raw.get("reason", "")).strip(),
        "risk_level": risk_level,
        "context_refs": _normalize_list(raw.get("context_refs", [])),
        "input_refs": _normalize_list(raw.get("input_refs", [])),
        "output_dir": output_dir,
        "requires_review": bool(raw.get("requires_review", False)),
        "escalation_policy": raw.get("escalation_policy", {}) if isinstance(raw.get("escalation_policy"), dict) else {},
        "created_at": str(raw.get("created_at", "")).strip() or utc_now_iso(),
        "input_payload": raw.get("input_payload", {}) if isinstance(raw.get("input_payload"), dict) else {},
        "approval_context": raw.get("approval_context", {}) if isinstance(raw.get("approval_context"), dict) else {},
    }


def _write_runtime_payload(path: Path, payload: dict[str, Any]) -> str:
    _atomic_write_json(path, payload)
    return str(path)


def _emit_dispatch_event(event_type: str, request: dict[str, Any], **payload: Any) -> None:
    trace_id = str(request.get("trace_id", "")).strip()
    task_id = str(request.get("task_id", "")).strip()
    if not trace_id or not task_id:
        return
    append_event(trace_id=trace_id, task_id=task_id, event_type=event_type, payload=payload)


def _handler_provider(request: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    task_payload = dict(request.get("input_payload", {}))
    if not task_payload:
        raise ValueError("missing_provider_input_payload")
    forced_provider = str(task_payload.pop("forced_provider", "")).strip().lower()
    output_path = output_dir / "provider_result.json"
    envelope = run_routed_task(task_payload, output_path=output_path, forced_provider=forced_provider)
    return {"ok": bool(envelope.get("ok", False)), "handler": "provider", "envelope": envelope, "result_path": str(output_path)}


def _handler_reviewer(request: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    del output_dir
    payload = dict(request.get("input_payload", {}))
    file_path = str(payload.get("file_path", "")).strip()
    if not file_path and request["input_refs"]:
        file_path = request["input_refs"][0]
    if not file_path:
        raise ValueError("missing_review_target")
    target = Path(file_path)
    if not target.is_absolute():
        target = (ROOT / target).resolve()
    include_dir = Path(str(payload.get("include_dir", ROOT)).strip())
    if not include_dir.is_absolute():
        include_dir = (ROOT / include_dir).resolve()
    report = run_review(
        file_path=target,
        include_dir=include_dir,
        review_goal=str(payload.get("review_goal", "Review this file for correctness, safety, and operational fragility.")),
        extra_context=str(payload.get("extra_context", "")),
        claude_model=str(payload.get("claude_model", "opus")),
        gemini_model=str(payload.get("gemini_model", "pro")),
        excerpt_chars=max(1000, _safe_int(payload.get("excerpt_chars", 4000), 4000)),
        timeout_sec=max(30.0, _safe_float(payload.get("timeout_sec", 600.0), 600.0)),
        max_rounds=min(2, max(1, _safe_int(request["escalation_policy"].get("max_review_rounds", 1), 1))),
    )
    review_ok = isinstance(report, dict) and bool(report.get("ok", True))
    return {"ok": review_ok, "handler": "reviewer", "review_report": report}


def _handler_memory(request: dict[str, Any], _output_dir: Path) -> dict[str, Any]:
    payload = dict(request.get("input_payload", {}))
    if not payload:
        raise ValueError("missing_memory_record")
    payload.setdefault("trace_id", request["trace_id"])
    payload.setdefault("task_id", request["task_id"])
    record = append_record(payload)
    return {"ok": True, "handler": "memory_recorder", "record": record}


def _handler_vision(request: dict[str, Any], _output_dir: Path) -> dict[str, Any]:
    refs = request["input_refs"]
    if not refs:
        raise ValueError("missing_vision_input_refs")
    results = [
        parse_vision_ref(task_id=request["task_id"], attempt=index, image_ref=ref)
        for index, ref in enumerate(refs, start=1)
    ]
    all_ok = all(isinstance(item, dict) and bool(item.get("ok", False)) for item in results)
    security_refs = [str(item.get("security_ref", "")).strip() for item in results if str(item.get("security_ref", "")).strip()]
    evidence_result_indexes = [idx for idx, item in enumerate(results) if isinstance(item.get("evidence_refs", {}), dict) and bool(item.get("evidence_refs"))]
    return {
        "ok": all_ok,
        "handler": "vision_parser",
        "results": results,
        "quarantined_any": any(bool((item.get("security_scan", {}) or {}).get("quarantined", False)) for item in results),
        "redacted_any": any(bool(item.get("ocr_redacted", False)) for item in results),
        "security_refs": security_refs,
        "evidence_refs_present": bool(evidence_result_indexes),
        "evidence_result_indexes": evidence_result_indexes,
    }


def _handler_desktop(request: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    payload = dict(request.get("input_payload", {}))
    payload.setdefault("trace_id", request["trace_id"])
    payload.setdefault("task_id", request["task_id"])
    payload.setdefault("risk_level", request["risk_level"])
    payload.setdefault("approval_context", request.get("approval_context", {}))
    if payload.get("action_type") == "DESKTOP_OBSERVE":
        params = payload.get("params")
        if isinstance(params, dict) and str(params.get("surface", "")).strip().lower() == "screenshot" and not str(params.get("path", "")).strip():
            params["path"] = str(output_dir / f"{request['request_id']}.png")
    result = run_desktop_action(payload)
    return {"ok": bool(result.get("ok", False)), "handler": "desktop_adapter", "result": result}


def _handler_legacy_task(request: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    payload = dict(request.get("input_payload", {}))
    if not payload:
        raise ValueError("missing_legacy_task_payload")
    task_path = output_dir / "legacy_task.json"
    _atomic_write_json(task_path, payload)
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_agn_task.py"), "--from-json-file", str(task_path)],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=max(30.0, _safe_float(request["escalation_policy"].get("timeout_sec", 300.0), 300.0)),
        check=False,
    )
    parsed = {}
    try:
        parsed = json.loads(str(completed.stdout or "").strip())
    except Exception:
        parsed = {}
    return {
        "ok": completed.returncode == 0 and isinstance(parsed, dict) and bool(parsed.get("ok", False)),
        "handler": "legacy_task",
        "return_code": int(completed.returncode),
        "stdout": parsed if parsed else str(completed.stdout or "").strip(),
        "stderr": str(completed.stderr or "").strip(),
    }


HANDLERS = {
    "provider": _handler_provider,
    "reviewer": _handler_reviewer,
    "memory_recorder": _handler_memory,
    "vision_parser": _handler_vision,
    "desktop_adapter": _handler_desktop,
    "legacy_task": _handler_legacy_task,
}


def _approval_is_valid(request: dict[str, Any]) -> bool:
    """Validate that a pre-approved request has a genuine, approved gate entry."""
    approval = request.get("approval_context", {})
    if not isinstance(approval, dict):
        return False
    decision = str(approval.get("decision", "")).strip().lower()
    gate_id = str(approval.get("gate_id", "")).strip()
    if decision != "approved" or not gate_id:
        return False
    # Verify the gate actually exists and has been approved
    from agn.core.policy_gate import effective_gate_state, load_gate_entry
    entry = load_gate_entry(gate_id)
    if entry is None:
        return False
    state = effective_gate_state(gate_id)
    return str(state.get("status", "")).strip().lower() == "approved"


def _write_result_payload(request: dict[str, Any], *, ok: bool, result: dict[str, Any], failure_class: str) -> dict[str, Any]:
    result_payload = {
        "ok": ok,
        "request_id": request["request_id"],
        "trace_id": request["trace_id"],
        "task_id": request["task_id"],
        "target": request["target"],
        "target_kind": request["target_kind"],
        "result": result,
        "failure_class": failure_class,
        "completed_at": utc_now_iso(),
    }
    result_path = RESULTS_DIR / f"{request['request_id']}.json"
    result_ref = _write_runtime_payload(result_path, result_payload)
    return {**result_payload, "result_ref": result_ref}


def _refresh_read_models() -> None:
    try:
        refresh_read_models()
    except Exception as exc:
        import sys

        print(f"[dispatcher] read model refresh failed: {type(exc).__name__}: {exc}", file=sys.stderr)


def dispatch_request(raw: dict[str, Any]) -> dict[str, Any]:
    ensure_dispatcher_dirs()
    request = normalize_dispatcher_request(raw)
    errors = validate_dispatcher_request(request)
    if errors:
        return {"ok": False, "failure_class": "schema_invalid", "errors": errors}

    output_dir = Path(request["output_dir"]).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    request_path = REQUESTS_DIR / f"{request['request_id']}.json"
    request_ref = _write_runtime_payload(request_path, request)
    _emit_dispatch_event(
        "DISPATCHER_REQUEST_CREATED",
        request,
        request_id=request["request_id"],
        target=request["target"],
        target_kind=request["target_kind"],
        request_ref=request_ref,
    )
    if not dispatcher_accepts_new_work():
        blocked_payload = _write_result_payload(
            request,
            ok=False,
            result={"ok": False, "status": "blocked", "reason": "emergency_stop_active"},
            failure_class="emergency_stop_active",
        )
        publish_message(
            {
                "from": "dispatcher_runtime",
                "to": request["caller"],
                "type": "dispatch.blocked",
                "topic": "dispatch.blocked",
                "summary": "dispatcher blocked by emergency stop",
                "payload_ref": blocked_payload["result_ref"],
                "priority": request["risk_level"],
                "ttl_sec": 0,
                "ack_required": False,
                "related_task": request["task_id"],
                "related_trace": request["trace_id"],
                "related_project": "AgenticNetwork",
            }
        )
        _emit_dispatch_event(
            "DISPATCHER_REQUEST_BLOCKED",
            request,
            request_id=request["request_id"],
            result_ref=blocked_payload["result_ref"],
            failure_class="emergency_stop_active",
        )
        _refresh_read_models()
        return {**blocked_payload, "request_ref": request_ref}

    try:
        gate_eval = evaluate_dispatch_request(request)
    except Exception:
        gate_eval = {"requires_gate": True, "rule_id": "evaluation_error_fallback"}
    if gate_eval.get("requires_gate") and not _approval_is_valid(request):
        gate = create_gate_entry(request=request, request_ref=request_ref, evaluation=gate_eval)
        gated_payload = _write_result_payload(
            request,
            ok=False,
            result={"ok": False, "status": "queued_for_approval", "gate_id": gate["gate_id"], "gate_ref": gate["gate_ref"]},
            failure_class="policy_gate_pending",
        )
        publish_message(
            {
                "from": "dispatcher_runtime",
                "to": "policy_gate",
                "type": "dispatch.gated",
                "topic": "dispatch.gated",
                "summary": f"{request['intent']} queued for admin approval",
                "payload_ref": gate["gate_ref"],
                "priority": request["risk_level"],
                "ttl_sec": 0,
                "ack_required": False,
                "related_task": request["task_id"],
                "related_trace": request["trace_id"],
                "related_project": "AgenticNetwork",
            }
        )
        _emit_dispatch_event(
            "DISPATCHER_REQUEST_GATED",
            request,
            request_id=request["request_id"],
            gate_id=gate["gate_id"],
            policy_rule_id=gate["policy_rule_id"],
            result_ref=gated_payload["result_ref"],
        )
        _refresh_read_models()
        return {**gated_payload, "request_ref": request_ref}

    request_message = publish_message(
        {
            "from": request["caller"],
            "to": request["target"],
            "type": "dispatch.request",
            "topic": f"dispatch.{request['target_kind']}",
            "summary": f"{request['intent']} for {request['target_kind']}",
            "payload_ref": request_ref,
            "priority": request["risk_level"],
            "ttl_sec": _safe_int(
                request["escalation_policy"].get("ttl_sec", 3600 if request["target_kind"] == "desktop_adapter" else 0),
                3600 if request["target_kind"] == "desktop_adapter" else 0,
            ),
            "ack_required": request["target_kind"] == "desktop_adapter",
            "related_task": request["task_id"],
            "related_trace": request["trace_id"],
            "related_project": "AgenticNetwork",
        }
    )
    _emit_dispatch_event(
        "DISPATCHER_REQUEST_ENQUEUED",
        request,
        request_id=request["request_id"],
        bus_message_id=request_message["id"],
    )

    handler = HANDLERS[request["target_kind"]]
    try:
        result = handler(request, output_dir)
        ok = bool(result.get("ok", False))
        failure_class = "" if ok else str(result.get("failure_class", "handler_failed")).strip() or "handler_failed"
    except Exception as exc:
        ok = False
        failure_class = getattr(exc, "failure_class", "handler_exception")  # type: ignore[attr-defined]
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "failure_class": str(failure_class)}

    result_payload = _write_result_payload(request, ok=ok, result=result, failure_class=failure_class)
    result_ref = result_payload["result_ref"]
    publish_message(
        {
            "from": request["target"],
            "to": request["caller"],
            "type": "dispatch.result",
            "topic": f"dispatch.{request['target_kind']}.result",
            "summary": "request completed" if ok else f"request failed:{failure_class}",
            "payload_ref": result_ref,
            "priority": request["risk_level"],
            "ttl_sec": 0,
            "ack_required": False,
            "related_task": request["task_id"],
            "related_trace": request["trace_id"],
            "related_project": "AgenticNetwork",
            "reply_to": request_message["id"],
        }
    )
    if request_message["ack_required"] and ok:
        acknowledge_message(request_message["id"], actor=request["target"])

    _emit_dispatch_event(
        "DISPATCHER_REQUEST_COMPLETED" if ok else "DISPATCHER_REQUEST_FAILED",
        request,
        request_id=request["request_id"],
        result_ref=result_ref,
        target=request["target"],
        target_kind=request["target_kind"],
        failure_class=failure_class,
    )
    _refresh_read_models()
    return {**result_payload, "request_ref": request_ref, "result_ref": result_ref}


def main() -> int:
    parser = argparse.ArgumentParser(description="Canonical dispatcher entrypoint for AGN runtime modules")
    sub = parser.add_subparsers(dest="command", required=True)

    dispatch_parser = sub.add_parser("dispatch")
    dispatch_parser.add_argument("--from-json-file", required=True)

    expire_parser = sub.add_parser("expire-bus")
    expire_parser.add_argument("--quiet", action="store_true")

    args = parser.parse_args()
    if args.command == "expire-bus":
        payload = {"ok": True, "expired": expire_messages()}
        if not args.quiet:
            print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0

    raw = json.loads(Path(args.from_json_file).read_text(encoding="utf-8"))
    result = dispatch_request(raw)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
