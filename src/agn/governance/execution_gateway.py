"""AGN governed execution gateway.

This is the real package implementation for the governed execution facade.
Legacy script entrypoints should re-export from here while active package code
can import this module directly.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from agn.dispatch.dispatcher import dispatch_request
from pointer_protocol import write_file_artifact


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _trim(text: str, *, limit: int = 160, default: str) -> str:
    cleaned = " ".join(str(text or "").strip().split())
    if not cleaned:
        cleaned = default
    return cleaned[:limit]


def _dispatch_meta(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_id": str(payload.get("request_id", "")).strip(),
        "request_ref": str(payload.get("request_ref", "")).strip(),
        "result_ref": str(payload.get("result_ref", "")).strip(),
        "failure_class": str(payload.get("failure_class", "")).strip(),
        "completed_at": str(payload.get("completed_at", "")).strip(),
    }


def _dispatch_error(payload: dict[str, Any]) -> str:
    result = payload.get("result", {})
    if isinstance(result, dict):
        if str(result.get("error", "")).strip():
            return str(result.get("error", "")).strip()
        if str(result.get("status", "")).strip():
            reason = str(result.get("reason", "")).strip()
            return f"{result['status']}:{reason}" if reason else str(result["status"])
        nested = result.get("result", {})
        if isinstance(nested, dict):
            if str(nested.get("error", "")).strip():
                return str(nested.get("error", "")).strip()
            if str(nested.get("status", "")).strip():
                reason = str(nested.get("reason", "")).strip()
                return f"{nested['status']}:{reason}" if reason else str(nested["status"])
    failure_class = str(payload.get("failure_class", "")).strip()
    return failure_class or "dispatch_failed"


def _base_request(
    *,
    caller: str,
    target: str,
    target_kind: str,
    intent: str,
    reason: str,
    risk_level: str,
    task_id: str,
    trace_id: str,
    input_payload: dict[str, Any] | None = None,
    input_refs: list[str] | None = None,
    context_refs: list[str] | None = None,
    output_dir: str = "",
    requires_review: bool = False,
    escalation_policy: dict[str, Any] | None = None,
    approval_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_task_id = str(task_id).strip() or f"{target_kind}-{uuid4().hex[:12]}"
    resolved_trace_id = str(trace_id).strip() or f"trace-{resolved_task_id}"
    return {
        "caller": str(caller).strip() or "agn_governed_execution",
        "target": str(target).strip(),
        "target_kind": str(target_kind).strip(),
        "intent": _trim(intent, default=f"{target_kind}_request"),
        "reason": _trim(reason, limit=240, default=f"governed_{target_kind}_request"),
        "risk_level": str(risk_level).strip().lower() or "medium",
        "task_id": resolved_task_id,
        "trace_id": resolved_trace_id,
        "input_payload": input_payload if isinstance(input_payload, dict) else {},
        "input_refs": list(input_refs or []),
        "context_refs": list(context_refs or []),
        "output_dir": str(output_dir).strip(),
        "requires_review": bool(requires_review),
        "escalation_policy": escalation_policy if isinstance(escalation_policy, dict) else {},
        "approval_context": approval_context if isinstance(approval_context, dict) else {},
        "created_at": utc_now_iso(),
    }


def dispatch_provider_task(
    task_payload: dict[str, Any],
    *,
    caller: str = "agn_governed_execution",
    task_id: str = "",
    trace_id: str = "",
    intent: str = "",
    reason: str = "",
    risk_level: str = "medium",
    context_refs: list[str] | None = None,
    output_dir: str = "",
    requires_review: bool = False,
    forced_provider: str = "",
) -> dict[str, Any]:
    payload = dict(task_payload or {})
    if forced_provider:
        payload["forced_provider"] = str(forced_provider).strip().lower()
    dispatch = dispatch_request(
        _base_request(
            caller=caller,
            target="provider_router",
            target_kind="provider",
            intent=intent or str(payload.get("instruction", "")) or "route_provider_task",
            reason=reason or "governed provider execution",
            risk_level=risk_level,
            task_id=task_id,
            trace_id=trace_id,
            input_payload=payload,
            context_refs=context_refs,
            output_dir=output_dir,
            requires_review=requires_review,
        )
    )
    handler_payload = dispatch.get("result", {}) if isinstance(dispatch.get("result", {}), dict) else {}
    envelope = handler_payload.get("envelope", {}) if isinstance(handler_payload.get("envelope", {}), dict) else {}
    ok = bool(dispatch.get("ok", False)) and bool(envelope.get("ok", False))
    return {
        "ok": ok,
        "dispatch_meta": _dispatch_meta(dispatch),
        "dispatch_result": dispatch if not ok else {},
        "envelope": envelope,
        "result_path": str(handler_payload.get("result_path", "")).strip(),
        "failure_class": str(dispatch.get("failure_class", "")).strip(),
        "error": "" if ok else _dispatch_error(dispatch),
    }


def dispatch_review_request(
    *,
    file_path: str,
    include_dir: str,
    review_goal: str,
    extra_context: str = "",
    claude_model: str = "opus",
    gemini_model: str = "pro",
    excerpt_chars: int = 4000,
    timeout_sec: float = 600.0,
    max_rounds: int = 1,
    caller: str = "agn_governed_execution",
    task_id: str = "",
    trace_id: str = "",
    intent: str = "run_flagship_review",
    reason: str = "governed review execution",
    risk_level: str = "medium",
    context_refs: list[str] | None = None,
) -> dict[str, Any]:
    dispatch = dispatch_request(
        _base_request(
            caller=caller,
            target="flagship_review",
            target_kind="reviewer",
            intent=intent,
            reason=reason,
            risk_level=risk_level,
            task_id=task_id,
            trace_id=trace_id,
            input_payload={
                "file_path": str(file_path).strip(),
                "include_dir": str(include_dir).strip(),
                "review_goal": str(review_goal).strip(),
                "extra_context": str(extra_context).strip(),
                "claude_model": str(claude_model).strip(),
                "gemini_model": str(gemini_model).strip(),
                "excerpt_chars": max(1000, int(excerpt_chars)),
                "timeout_sec": max(30.0, float(timeout_sec)),
            },
            context_refs=context_refs,
            escalation_policy={"max_review_rounds": min(2, max(1, int(max_rounds)))},
        )
    )
    handler_payload = dispatch.get("result", {}) if isinstance(dispatch.get("result", {}), dict) else {}
    report = handler_payload.get("review_report", {}) if isinstance(handler_payload.get("review_report", {}), dict) else {}
    ok = bool(dispatch.get("ok", False)) and bool(report)
    return {
        "ok": ok,
        "dispatch_meta": _dispatch_meta(dispatch),
        "dispatch_result": dispatch if not ok else {},
        "review_report": report,
        "failure_class": str(dispatch.get("failure_class", "")).strip(),
        "error": "" if ok else _dispatch_error(dispatch),
    }


def dispatch_memory_record(
    record: dict[str, Any],
    *,
    caller: str = "agn_governed_execution",
    task_id: str = "",
    trace_id: str = "",
    intent: str = "record_memory",
    reason: str = "governed memory append",
    risk_level: str = "low",
    context_refs: list[str] | None = None,
) -> dict[str, Any]:
    dispatch = dispatch_request(
        _base_request(
            caller=caller,
            target="memory_recorder",
            target_kind="memory_recorder",
            intent=intent,
            reason=reason,
            risk_level=risk_level,
            task_id=task_id,
            trace_id=trace_id,
            input_payload=dict(record or {}),
            context_refs=context_refs,
        )
    )
    handler_payload = dispatch.get("result", {}) if isinstance(dispatch.get("result", {}), dict) else {}
    record_payload = handler_payload.get("record", {}) if isinstance(handler_payload.get("record", {}), dict) else {}
    ok = bool(dispatch.get("ok", False)) and bool(record_payload)
    return {
        "ok": ok,
        "dispatch_meta": _dispatch_meta(dispatch),
        "dispatch_result": dispatch if not ok else {},
        "record": record_payload,
        "failure_class": str(dispatch.get("failure_class", "")).strip(),
        "error": "" if ok else _dispatch_error(dispatch),
    }


def dispatch_vision_refs(
    input_refs: list[str],
    *,
    caller: str = "agn_governed_execution",
    task_id: str = "",
    trace_id: str = "",
    intent: str = "inspect_visual",
    reason: str = "governed vision parse",
    risk_level: str = "low",
    context_refs: list[str] | None = None,
) -> dict[str, Any]:
    dispatch = dispatch_request(
        _base_request(
            caller=caller,
            target="vision_parser",
            target_kind="vision_parser",
            intent=intent,
            reason=reason,
            risk_level=risk_level,
            task_id=task_id,
            trace_id=trace_id,
            input_refs=list(input_refs or []),
            context_refs=context_refs,
        )
    )
    handler_payload = dispatch.get("result", {}) if isinstance(dispatch.get("result", {}), dict) else {}
    results = handler_payload.get("results", []) if isinstance(handler_payload.get("results", []), list) else []
    ok = bool(dispatch.get("ok", False)) and bool(results)
    return {
        "ok": ok,
        "dispatch_meta": _dispatch_meta(dispatch),
        "dispatch_result": dispatch if not ok else {},
        "results": results,
        "quarantined_any": bool(handler_payload.get("quarantined_any", False)),
        "redacted_any": bool(handler_payload.get("redacted_any", False)),
        "security_refs": list(handler_payload.get("security_refs", [])) if isinstance(handler_payload.get("security_refs", []), list) else [],
        "evidence_refs_present": bool(handler_payload.get("evidence_refs_present", False)),
        "evidence_result_indexes": list(handler_payload.get("evidence_result_indexes", [])) if isinstance(handler_payload.get("evidence_result_indexes", []), list) else [],
        "failure_class": str(dispatch.get("failure_class", "")).strip(),
        "error": "" if ok else _dispatch_error(dispatch),
    }


def dispatch_desktop_action(
    action: dict[str, Any],
    *,
    caller: str = "agn_governed_execution",
    task_id: str = "",
    trace_id: str = "",
    intent: str = "",
    reason: str = "",
    risk_level: str = "",
    context_refs: list[str] | None = None,
    approval_context: dict[str, Any] | None = None,
    output_dir: str = "",
) -> dict[str, Any]:
    payload = dict(action or {})
    effective_trace_id = str(trace_id).strip() or str(payload.get("trace_id", "")).strip()
    effective_task_id = str(task_id).strip() or str(payload.get("task_id", "")).strip()
    effective_risk_level = str(risk_level).strip().lower() or str(payload.get("risk_level", "medium")).strip().lower() or "medium"
    action_type = str(payload.get("action_type", "")).strip() or "desktop_action"
    dispatch = dispatch_request(
        _base_request(
            caller=caller,
            target="desktop_adapter",
            target_kind="desktop_adapter",
            intent=intent or action_type.lower(),
            reason=reason or "governed desktop action",
            risk_level=effective_risk_level,
            task_id=effective_task_id,
            trace_id=effective_trace_id,
            input_payload=payload,
            context_refs=context_refs,
            output_dir=output_dir,
            approval_context=approval_context,
        )
    )
    handler_payload = dispatch.get("result", {}) if isinstance(dispatch.get("result", {}), dict) else {}
    desktop_result = handler_payload.get("result", {}) if isinstance(handler_payload.get("result", {}), dict) else {}
    ok = bool(dispatch.get("ok", False)) and bool(desktop_result.get("ok", False))
    return {
        "ok": ok,
        "dispatch_meta": _dispatch_meta(dispatch),
        "dispatch_result": dispatch if not ok else {},
        "result": desktop_result,
        "failure_class": str(dispatch.get("failure_class", "")).strip(),
        "error": "" if ok else _dispatch_error(dispatch),
    }


def describe_gateway() -> dict[str, Any]:
    return {
        "schema_version": "agn.governed_execution_gateway.v1",
        "generated_at": utc_now_iso(),
        "purpose": "Route active provider, review, memory, vision, and desktop execution through the canonical dispatcher.",
        "phase_alignment": "phase_3_gradual_implementation_migration",
        "origin_phase": "phase_2_governance_enforcement_boundary",
        "package_path": "agn.governance.execution_gateway",
        "legacy_script_shim": "scripts/agn_governed_execution.py",
        "typed_operations": [
            "dispatch_provider_task",
            "dispatch_review_request",
            "dispatch_memory_record",
            "dispatch_vision_refs",
            "dispatch_desktop_action",
        ],
        "operator_guidance": [
            "Use this gateway from active AGN modules instead of directly importing execution handlers.",
            "Keep direct handler imports for the dispatcher itself, tests, validation code, or explicit compatibility layers.",
            "Treat this gateway as a Phase 2 boundary surface carried into Phase 3 package migration, not as a second dispatcher.",
        ],
        "cli_commands": ["show", "provider", "review", "memory", "vision", "desktop"],
    }


def _load_json_payload(args: argparse.Namespace) -> dict[str, Any]:
    from_json_file = str(getattr(args, "from_json_file", "") or "").strip()
    from_stdin = bool(getattr(args, "from_stdin", False))
    if from_json_file and from_stdin:
        raise ValueError("use_only_one_of_from_json_file_or_from_stdin")
    if from_json_file:
        payload = json.loads(Path(from_json_file).read_text(encoding="utf-8"))
    elif from_stdin:
        payload = json.load(sys.stdin)
    else:
        raise ValueError("json_input_required")
    if not isinstance(payload, dict):
        raise ValueError("json_input_must_be_object")
    return payload


def _media_type_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".tiff": "image/tiff",
        ".tif": "image/tiff",
        ".pdf": "application/pdf",
    }.get(suffix, "application/octet-stream")


def _register_input_artifact(*, task_id: str, attempt: int, image_path: str, artifact_id: str = "vision_input") -> dict[str, Any]:
    path = Path(str(image_path)).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"image_path_not_found:{path}")
    artifact = write_file_artifact(
        task_id=task_id,
        attempt=max(1, int(attempt)),
        artifact_id=artifact_id,
        source_path=path,
        media_type=_media_type_for_path(path),
        filename=path.name,
        source="agn_governed_execution",
    )
    return {
        "artifact_id": artifact.artifact_id,
        "image_ref": artifact.ref,
        "sha256": artifact.sha256,
        "bytes": artifact.bytes,
        "media_type": artifact.media_type,
        "rel_path": artifact.rel_path,
    }


def cmd_provider(args: argparse.Namespace) -> int:
    payload = _load_json_payload(args)
    response = dispatch_provider_task(
        payload,
        caller="agn_governed_execution_cli",
        task_id=str(getattr(args, "task_id", "") or "").strip(),
        trace_id=str(getattr(args, "trace_id", "") or "").strip(),
        intent=str(getattr(args, "intent", "") or "").strip(),
        reason=str(getattr(args, "reason", "") or "").strip(),
        risk_level=str(getattr(args, "risk_level", "") or "medium").strip(),
        output_dir=str(getattr(args, "output_dir", "") or "").strip(),
        requires_review=bool(getattr(args, "requires_review", False)),
        forced_provider=str(getattr(args, "force_provider", "") or "").strip(),
    )
    print(json.dumps(response, ensure_ascii=True, indent=2))
    return 0 if bool(response.get("ok", False)) else 1


def cmd_review(args: argparse.Namespace) -> int:
    response = dispatch_review_request(
        file_path=str(args.file).strip(),
        include_dir=str(args.include_dir).strip(),
        review_goal=str(args.goal).strip(),
        extra_context=str(args.extra_context).strip(),
        claude_model=str(args.claude_model).strip(),
        gemini_model=str(args.gemini_model).strip(),
        excerpt_chars=max(1000, int(args.excerpt_chars)),
        timeout_sec=max(30.0, float(args.timeout_sec)),
        max_rounds=min(2, max(1, int(args.max_rounds))),
        caller="agn_governed_execution_cli",
        task_id=str(getattr(args, "task_id", "") or "").strip(),
        trace_id=str(getattr(args, "trace_id", "") or "").strip(),
        intent=str(getattr(args, "intent", "") or "run_flagship_review").strip(),
        reason=str(getattr(args, "reason", "") or "governed review execution").strip(),
        risk_level=str(getattr(args, "risk_level", "") or "medium").strip(),
    )
    print(json.dumps(response, ensure_ascii=True, indent=2))
    return 0 if bool(response.get("ok", False)) else 1


def cmd_memory(args: argparse.Namespace) -> int:
    payload = _load_json_payload(args)
    response = dispatch_memory_record(
        payload,
        caller="agn_governed_execution_cli",
        task_id=str(getattr(args, "task_id", "") or "").strip(),
        trace_id=str(getattr(args, "trace_id", "") or "").strip(),
        intent=str(getattr(args, "intent", "") or "record_memory").strip(),
        reason=str(getattr(args, "reason", "") or "governed memory append").strip(),
        risk_level=str(getattr(args, "risk_level", "") or "low").strip(),
    )
    print(json.dumps(response, ensure_ascii=True, indent=2))
    return 0 if bool(response.get("ok", False)) else 1


def cmd_vision(args: argparse.Namespace) -> int:
    task_id = str(args.task_id).strip()
    attempt = max(1, int(args.attempt))
    image_ref = str(getattr(args, "image_ref", "") or "").strip()
    registered: dict[str, Any] | None = None
    if not image_ref:
        registered = _register_input_artifact(
            task_id=task_id,
            attempt=attempt,
            image_path=str(getattr(args, "image_path", "") or "").strip(),
            artifact_id=str(getattr(args, "artifact_id", "") or "vision_input").strip(),
        )
        image_ref = str(registered["image_ref"]).strip()
    response = dispatch_vision_refs(
        [image_ref],
        caller="agn_governed_execution_cli",
        task_id=task_id,
        trace_id=str(getattr(args, "trace_id", "") or "").strip(),
        intent=str(getattr(args, "intent", "") or "inspect_visual").strip(),
        reason=str(getattr(args, "reason", "") or "governed vision parse").strip(),
        risk_level=str(getattr(args, "risk_level", "") or "low").strip(),
    )
    if registered:
        response["registered_input"] = registered
    print(json.dumps(response, ensure_ascii=True, indent=2))
    return 0 if bool(response.get("ok", False)) else 1


def cmd_desktop(args: argparse.Namespace) -> int:
    payload: dict[str, Any]
    if str(getattr(args, "from_json_file", "") or "").strip() or bool(getattr(args, "from_stdin", False)):
        payload = _load_json_payload(args)
    else:
        payload = {
            "action_type": "DESKTOP_OBSERVE",
            "params": {"surface": str(getattr(args, "surface", "") or "status").strip() or "status"},
        }
    response = dispatch_desktop_action(
        payload,
        caller="agn_governed_execution_cli",
        task_id=str(getattr(args, "task_id", "") or "").strip(),
        trace_id=str(getattr(args, "trace_id", "") or "").strip(),
        intent=str(getattr(args, "intent", "") or "").strip(),
        reason=str(getattr(args, "reason", "") or "").strip(),
        risk_level=str(getattr(args, "risk_level", "") or "").strip(),
        output_dir=str(getattr(args, "output_dir", "") or "").strip(),
    )
    print(json.dumps(response, ensure_ascii=True, indent=2))
    return 0 if bool(response.get("ok", False)) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Describe or invoke AGN's governed execution gateway for active execution surfaces.")
    sub = parser.add_subparsers(dest="command", required=False)

    show = sub.add_parser("show", help="Describe the governed execution gateway")
    show.set_defaults(func=lambda _args: 0)

    provider = sub.add_parser("provider", help="Dispatch provider work through the governed gateway")
    provider.add_argument("--from-json-file", help="Read task payload JSON from file")
    provider.add_argument("--from-stdin", action="store_true", help="Read task payload JSON from stdin")
    provider.add_argument("--task-id", default="")
    provider.add_argument("--trace-id", default="")
    provider.add_argument("--intent", default="")
    provider.add_argument("--reason", default="")
    provider.add_argument("--risk-level", default="medium")
    provider.add_argument("--output-dir", default="")
    provider.add_argument("--requires-review", action="store_true")
    provider.add_argument("--force-provider", choices=["qwen_local", "deepseek", "gemini", "claude", "vertex_local"], default="")
    provider.set_defaults(func=cmd_provider)

    review = sub.add_parser("review", help="Dispatch flagship review through the governed gateway")
    review.add_argument("--file", required=True)
    review.add_argument("--include-dir", default=str(ROOT))
    review.add_argument("--goal", default="Review this file for correctness, safety, and operational fragility.")
    review.add_argument("--extra-context", default="")
    review.add_argument("--claude-model", default="opus")
    review.add_argument("--gemini-model", default="pro")
    review.add_argument("--excerpt-chars", type=int, default=4000)
    review.add_argument("--timeout-sec", type=float, default=600.0)
    review.add_argument("--max-rounds", type=int, default=1)
    review.add_argument("--task-id", default="")
    review.add_argument("--trace-id", default="")
    review.add_argument("--intent", default="run_flagship_review")
    review.add_argument("--reason", default="governed review execution")
    review.add_argument("--risk-level", default="medium")
    review.set_defaults(func=cmd_review)

    memory = sub.add_parser("memory", help="Dispatch append-only memory recording through the governed gateway")
    memory.add_argument("--from-json-file", help="Read memory record JSON from file")
    memory.add_argument("--from-stdin", action="store_true", help="Read memory record JSON from stdin")
    memory.add_argument("--task-id", default="")
    memory.add_argument("--trace-id", default="")
    memory.add_argument("--intent", default="record_memory")
    memory.add_argument("--reason", default="governed memory append")
    memory.add_argument("--risk-level", default="low")
    memory.set_defaults(func=cmd_memory)

    vision = sub.add_parser("vision", help="Dispatch visual parsing through the governed gateway")
    vision.add_argument("--task-id", required=True)
    vision.add_argument("--attempt", type=int, default=1)
    inputs = vision.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--image-ref")
    inputs.add_argument("--image-path")
    vision.add_argument("--artifact-id", default="vision_input")
    vision.add_argument("--trace-id", default="")
    vision.add_argument("--intent", default="inspect_visual")
    vision.add_argument("--reason", default="governed vision parse")
    vision.add_argument("--risk-level", default="low")
    vision.set_defaults(func=cmd_vision)

    desktop = sub.add_parser("desktop", help="Dispatch desktop observation or action through the governed gateway")
    desktop.add_argument("--from-json-file", help="Read desktop action JSON from file")
    desktop.add_argument("--from-stdin", action="store_true", help="Read desktop action JSON from stdin")
    desktop.add_argument("--surface", default="status", help="Convenience observe surface when no JSON payload is provided")
    desktop.add_argument("--task-id", default="")
    desktop.add_argument("--trace-id", default="")
    desktop.add_argument("--intent", default="")
    desktop.add_argument("--reason", default="")
    desktop.add_argument("--risk-level", default="")
    desktop.add_argument("--output-dir", default="")
    desktop.set_defaults(func=cmd_desktop)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not getattr(args, "command", "") or args.command == "show":
        print(json.dumps(describe_gateway(), ensure_ascii=True, indent=2))
        return 0
    return int(args.func(args))
