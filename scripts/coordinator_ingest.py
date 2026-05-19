#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agn_api.ssot_store import SSOTStore
from agn_api.task_engine import derive_status
try:
    from provider_registry import load_registry, resolve_executor_provider, resolve_reviewer_provider
except ImportError:  # pragma: no cover - package import fallback
    from scripts.provider_registry import load_registry, resolve_executor_provider, resolve_reviewer_provider

try:
    from agent_runner import (
        PATHS,
        append_audit,
        atomic_write_json,
        dispatch_path,
        ensure_dirs,
        load_json,
        utc_now_iso,
    )
except ImportError:  # pragma: no cover - package import fallback
    from scripts.agent_runner import (
        PATHS,
        append_audit,
        atomic_write_json,
        dispatch_path,
        ensure_dirs,
        load_json,
        utc_now_iso,
    )
try:
    from pointer_protocol import ref_to_artifact_entry, write_text_artifact
except ImportError:  # pragma: no cover - package import fallback
    from scripts.pointer_protocol import ref_to_artifact_entry, write_text_artifact

DEFAULT_ACCEPTANCE_CRITERIA: list[dict[str, str]] = [
    {"id": "AC-1", "text": "executor must write ack with exact criteria echo"},
    {"id": "AC-2", "text": "executor must write result with minimum 5 work logs"},
    {"id": "AC-3", "text": "reviewer issues must reference criterion ids with valid evidence index"},
]
_PROVIDER_REGISTRY = load_registry()
DEFAULT_EXECUTOR_PROVIDER = resolve_executor_provider(
    (os.getenv("EXECUTOR_PROVIDER", "") or "").strip().lower(),
    _PROVIDER_REGISTRY,
)
DEFAULT_REVIEWER_PROVIDER = resolve_reviewer_provider(
    (os.getenv("REVIEWER_PROVIDER", "") or "").strip().lower(),
    _PROVIDER_REGISTRY,
)
VALID_TASK_KINDS = {"protocol", "repo"}
VALID_RISK_LEVELS = {"low", "medium", "high"}
VALID_SIDE_EFFECT_LEVELS = {"read_only", "local_write", "external_publish"}
DEFAULT_RISK_LEVEL = "low"
DEFAULT_SIDE_EFFECT_LEVEL = "read_only"
_DISPATCH_REQUEST_INLINE_LIMIT = max(512, int(os.getenv("AGN_DISPATCH_REQUEST_INLINE_LIMIT", "4096") or "4096"))
_DISPATCH_REQUEST_SUMMARY_LIMIT = max(120, int(os.getenv("AGN_DISPATCH_REQUEST_SUMMARY_LIMIT", "480") or "480"))


def _norm_str(value: Any) -> str:
    return str(value or "").strip()


def _normalize_criteria_list(raw: list[Any]) -> list[dict[str, str]]:
    if not raw:
        raise ValueError("acceptance_criteria must be a non-empty list")

    normalized: list[dict[str, str]] = []
    for idx, item in enumerate(raw, start=1):
        if isinstance(item, dict):
            criterion_id = _norm_str(item.get("id")) or f"AC-{idx}"
            text = _norm_str(item.get("text"))
            if not text:
                raise ValueError(f"acceptance_criteria[{idx-1}] missing text")
            normalized.append({"id": criterion_id, "text": text})
            continue
        if isinstance(item, str):
            text = item.strip()
            if not text:
                raise ValueError(f"acceptance_criteria[{idx-1}] cannot be empty string")
            normalized.append({"id": f"AC-{idx}", "text": text})
            continue
        raise ValueError(f"acceptance_criteria[{idx-1}] must be object or string")

    return normalized


def parse_criteria(criteria_json: str | None, criterion_items: list[str]) -> list[dict[str, str]]:
    if criteria_json:
        parsed = json.loads(criteria_json)
        if not isinstance(parsed, list):
            raise ValueError("criteria_json must be a JSON array")
        return _normalize_criteria_list(parsed)

    if criterion_items:
        criteria: list[dict[str, str]] = []
        for idx, item in enumerate(criterion_items):
            if ":" not in item:
                raise ValueError(
                    f"criterion[{idx}] must use 'ID:TEXT' format, received: {item!r}"
                )
            criterion_id, text = item.split(":", 1)
            criterion_id = criterion_id.strip()
            text = text.strip()
            if not criterion_id or not text:
                raise ValueError(f"criterion[{idx}] has empty id/text")
            criteria.append({"id": criterion_id, "text": text})
        return criteria

    return [dict(item) for item in DEFAULT_ACCEPTANCE_CRITERIA]


def _normalize_reviewer_provider(raw: str) -> str:
    return resolve_reviewer_provider(_norm_str(raw).lower(), _PROVIDER_REGISTRY)


def _normalize_executor_provider(raw: str) -> str:
    return resolve_executor_provider(_norm_str(raw).lower(), _PROVIDER_REGISTRY)


def _normalize_risk_level(raw: str) -> str:
    candidate = _norm_str(raw).lower() or DEFAULT_RISK_LEVEL
    if candidate not in VALID_RISK_LEVELS:
        return DEFAULT_RISK_LEVEL
    return candidate


def _normalize_side_effect_level(raw: str) -> str:
    candidate = _norm_str(raw).lower() or DEFAULT_SIDE_EFFECT_LEVEL
    if candidate not in VALID_SIDE_EFFECT_LEVELS:
        return DEFAULT_SIDE_EFFECT_LEVEL
    return candidate


def _summarize_request_text(text: str) -> str:
    raw = _norm_str(text)
    if len(raw) <= _DISPATCH_REQUEST_SUMMARY_LIMIT:
        return raw
    return raw[: _DISPATCH_REQUEST_SUMMARY_LIMIT - 24] + "...<truncated-summary>..."


def _infer_task_kind(task_kind: str, source: str, repo_path: str, work_branch: str) -> str:
    raw = _norm_str(task_kind).lower()
    if raw in VALID_TASK_KINDS:
        return raw

    source_norm = _norm_str(source).lower()
    if source_norm in {"agn_smoke", "smoke", "protocol"}:
        return "protocol"
    if _norm_str(repo_path) and _norm_str(work_branch):
        return "repo"
    return "protocol"


def _validate_task_contract(task_kind: str, repo_path: str, work_branch: str) -> None:
    if task_kind == "repo":
        if not _norm_str(repo_path):
            raise ValueError("repo task missing repo_path")
        if not _norm_str(work_branch):
            raise ValueError("repo task missing work_branch")


def prepare_ssot_task(
    *,
    store: SSOTStore,
    task_id: str,
    source: str,
    request_text: str,
    correlation_id: str,
    acceptance_criteria: list[dict[str, str]],
    task_kind: str,
    repo_path: str,
    work_branch: str,
    executor_provider: str,
    reviewer_provider: str,
    chat_id: str,
    message_id: str,
    risk_level: str,
    side_effect_level: str,
) -> dict[str, Any]:
    now = utc_now_iso()

    base: dict[str, Any] = {
        "id": task_id,
        "source": source,
        "request_text": request_text,
        "review_requested": True,
        "decision": None,
        "status": "pending",
        "correlation_id": correlation_id,
        "acceptance_criteria": acceptance_criteria,
        "task_kind": task_kind,
        "repo_path": repo_path,
        "work_branch": work_branch,
        "executor_provider": executor_provider,
        "reviewer_provider": reviewer_provider,
        "risk_level": risk_level,
        "side_effect_level": side_effect_level,
        "agn_managed": True,
    }
    if chat_id:
        base["chat_id"] = chat_id
    if message_id:
        base["message_id"] = message_id

    # Use locked_update for atomic read-modify-write on existing tasks.
    with store.locked_update(task_id) as existing:
        if existing is None:
            # New task — create fresh.
            base["created_at"] = now
            base["qa_retry_count"] = 0
            base["lock_state"] = "active"
            base["lock_reason"] = ""
            base["locked_at"] = ""
            base["allow_external_publish"] = False
            base["admin_approved"] = False
            base["status"] = derive_status(base)
            store.save_task(base)
            append_audit(
                action="coordinator_task_created",
                task_id=task_id,
                route="/agn/coordinator/ingest",
                status=200,
                source=source,
                correlation_id=correlation_id,
            )
            return base

        # Existing task — merge under lock.
        existing.update(base)
        if "created_at" not in existing:
            existing["created_at"] = now
        if "qa_retry_count" not in existing:
            existing["qa_retry_count"] = 0
        if not _norm_str(existing.get("lock_state", "")).lower():
            existing["lock_state"] = "active"
        existing.setdefault("lock_reason", "")
        existing.setdefault("locked_at", "")
        if "allow_external_publish" not in existing:
            existing["allow_external_publish"] = False
        if "admin_approved" not in existing:
            existing["admin_approved"] = False
        existing["status"] = derive_status(existing)
        # existing is auto-saved by locked_update context manager.
        append_audit(
            action="coordinator_task_refreshed",
            task_id=task_id,
            route="/agn/coordinator/ingest",
            status=200,
            source=source,
            correlation_id=correlation_id,
        )
        return dict(existing)


def create_dispatch(
    *,
    task_id: str,
    correlation_id: str,
    source: str,
    acceptance_criteria: list[dict[str, str]],
    request_text: str,
    task_kind: str,
    repo_path: str,
    work_branch: str,
    executor_provider: str,
    reviewer_provider: str,
    chat_id: str,
    message_id: str,
    risk_level: str,
    side_effect_level: str,
    attempt: int | None,
) -> dict[str, Any]:
    dispatch_file = dispatch_path(task_id)
    existing_payload: dict[str, Any] = {}
    next_attempt = 1
    if attempt is not None:
        next_attempt = max(1, int(attempt))
    elif dispatch_file.exists():
        try:
            existing_payload = load_json(dispatch_file)
            next_attempt = int(existing_payload.get("attempt", 0)) + 1
        except Exception:
            next_attempt = 1
            existing_payload = {}

    payload = {
        "task_id": task_id,
        "correlation_id": correlation_id,
        "attempt": next_attempt,
        "acceptance_criteria": acceptance_criteria,
        "task_kind": task_kind,
        "lazy_loading_protocol": "pointer_v1",
    }

    incoming_context: dict[str, str] = {
        "source": _norm_str(source),
        "repo_path": _norm_str(repo_path),
        "work_branch": _norm_str(work_branch),
        "executor_provider": _norm_str(executor_provider),
        "reviewer_provider": _norm_str(reviewer_provider),
        "chat_id": _norm_str(chat_id),
        "message_id": _norm_str(message_id),
        "risk_level": _norm_str(risk_level),
        "side_effect_level": _norm_str(side_effect_level),
    }

    context_conflicts: list[str] = []
    for key, incoming in incoming_context.items():
        existing = _norm_str(existing_payload.get(key, "")) if isinstance(existing_payload, dict) else ""
        if incoming:
            payload[key] = incoming
            if existing and existing != incoming:
                context_conflicts.append(key)
        elif existing:
            payload[key] = existing

    artifact_refs: list[dict[str, Any]] = []
    request_ref = ""
    try:
        instruction_ref = write_text_artifact(
            task_id=task_id,
            attempt=next_attempt,
            artifact_id="instructions",
            content=_norm_str(request_text),
            media_type="text/markdown",
            filename="instructions.md",
            source="coordinator",
        )
        artifact_refs.append(ref_to_artifact_entry(instruction_ref))
        request_ref = instruction_ref.ref
    except Exception as exc:
        append_audit(
            action="dispatch_artifact_write_failed",
            task_id=task_id,
            route="/dispatch",
            status=500,
            correlation_id=correlation_id,
            attempt=next_attempt,
            error=type(exc).__name__,
        )

    normalized_request = _norm_str(request_text)
    payload["request_summary"] = _summarize_request_text(normalized_request)
    if request_ref:
        payload["request_text_ref"] = request_ref
    if normalized_request and len(normalized_request) <= _DISPATCH_REQUEST_INLINE_LIMIT:
        payload["request_text"] = normalized_request
    else:
        payload.pop("request_text", None)
        if normalized_request:
            payload["request_text_mode"] = "ref_only"

    if artifact_refs:
        payload["artifact_refs"] = artifact_refs

    atomic_write_json(dispatch_file, payload)
    append_audit(
        action="dispatch_created",
        task_id=task_id,
        route="/dispatch",
        status=200,
        correlation_id=correlation_id,
        attempt=next_attempt,
        criteria_count=len(acceptance_criteria),
        task_kind=task_kind,
        repo_path=payload.get("repo_path", ""),
        work_branch=payload.get("work_branch", ""),
        executor_provider=payload.get("executor_provider", ""),
        reviewer_provider=payload.get("reviewer_provider", ""),
        chat_id=payload.get("chat_id", ""),
        message_id=payload.get("message_id", ""),
        risk_level=payload.get("risk_level", ""),
        side_effect_level=payload.get("side_effect_level", ""),
        artifact_ref_count=len(payload.get("artifact_refs", []) or []),
        request_summary_len=len(str(payload.get("request_summary", ""))),
        request_inline_len=len(str(payload.get("request_text", ""))),
        request_text_mode=str(payload.get("request_text_mode", "inline")),
    )
    if context_conflicts:
        append_audit(
            action="dispatch_context_conflict",
            task_id=task_id,
            route="/dispatch",
            status=200,
            correlation_id=correlation_id,
            attempt=next_attempt,
            fields=context_conflicts,
            note="incoming_overrode_existing_values",
        )
    return payload


def run(
    *,
    task_id: str,
    request_text: str,
    source: str,
    correlation_id: str | None,
    criteria_json: str | None,
    criterion_items: list[str],
    task_kind: str,
    repo_path: str,
    work_branch: str,
    executor_provider: str,
    reviewer_provider: str,
    chat_id: str,
    message_id: str,
    risk_level: str,
    side_effect_level: str,
    attempt: int | None,
) -> dict[str, Any]:
    ensure_dirs()
    store = SSOTStore(PATHS.ssot_dir)

    normalized_source = _norm_str(source) or "coordinator"
    normalized_request_text = _norm_str(request_text)
    normalized_repo_path = _norm_str(repo_path)
    normalized_work_branch = _norm_str(work_branch)
    normalized_executor_provider = _normalize_executor_provider(executor_provider)
    normalized_reviewer_provider = _normalize_reviewer_provider(reviewer_provider)
    normalized_chat_id = _norm_str(chat_id)
    normalized_message_id = _norm_str(message_id)
    normalized_risk_level = _normalize_risk_level(risk_level)
    normalized_side_effect_level = _normalize_side_effect_level(side_effect_level)

    normalized_task_kind = _infer_task_kind(
        task_kind=task_kind,
        source=normalized_source,
        repo_path=normalized_repo_path,
        work_branch=normalized_work_branch,
    )
    _validate_task_contract(normalized_task_kind, normalized_repo_path, normalized_work_branch)

    existing_task = store.get_task(task_id)
    existing_correlation = ""
    if isinstance(existing_task, dict):
        existing_correlation = _norm_str(existing_task.get("correlation_id"))
    correlation = _norm_str(correlation_id) or existing_correlation or f"corr-{uuid4().hex[:12]}"
    criteria = parse_criteria(criteria_json, criterion_items)

    task = prepare_ssot_task(
        store=store,
        task_id=task_id,
        source=normalized_source,
        request_text=normalized_request_text,
        correlation_id=correlation,
        acceptance_criteria=criteria,
        task_kind=normalized_task_kind,
        repo_path=normalized_repo_path,
        work_branch=normalized_work_branch,
        executor_provider=normalized_executor_provider,
        reviewer_provider=normalized_reviewer_provider,
        chat_id=normalized_chat_id,
        message_id=normalized_message_id,
        risk_level=normalized_risk_level,
        side_effect_level=normalized_side_effect_level,
    )
    dispatch = create_dispatch(
        task_id=task_id,
        correlation_id=correlation,
        source=normalized_source,
        acceptance_criteria=criteria,
        request_text=normalized_request_text,
        task_kind=normalized_task_kind,
        repo_path=normalized_repo_path,
        work_branch=normalized_work_branch,
        executor_provider=normalized_executor_provider,
        reviewer_provider=normalized_reviewer_provider,
        chat_id=normalized_chat_id,
        message_id=normalized_message_id,
        risk_level=normalized_risk_level,
        side_effect_level=normalized_side_effect_level,
        attempt=attempt,
    )

    return {
        "ok": True,
        "task_id": task_id,
        "attempt": dispatch["attempt"],
        "correlation_id": correlation,
        "criteria_count": len(criteria),
        "status": task.get("status"),
        "task_kind": normalized_task_kind,
        "repo_path": dispatch.get("repo_path", ""),
        "work_branch": dispatch.get("work_branch", ""),
        "executor_provider": dispatch.get("executor_provider", ""),
        "reviewer_provider": dispatch.get("reviewer_provider", ""),
        "chat_id": dispatch.get("chat_id", ""),
        "message_id": dispatch.get("message_id", ""),
        "risk_level": dispatch.get("risk_level", ""),
        "side_effect_level": dispatch.get("side_effect_level", ""),
        "artifact_ref_count": len(dispatch.get("artifact_refs", []) or []),
    }


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.from_json_file and args.from_stdin:
        raise ValueError("use only one of --from-json-file or --from-stdin")

    if args.from_json_file:
        return json.loads(Path(args.from_json_file).read_text(encoding="utf-8"))
    if args.from_stdin:
        return json.load(sys.stdin)
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Create/refresh SSOT task and dispatch file")
    parser.add_argument("--task-id")
    parser.add_argument("--request-text", default="agent integration request")
    parser.add_argument("--source", default="coordinator")
    parser.add_argument("--correlation-id")
    parser.add_argument("--criteria-json", help="JSON list with {id,text} entries")
    parser.add_argument(
        "--criterion",
        action="append",
        default=[],
        help="Single criterion as ID:TEXT; may be provided multiple times",
    )
    parser.add_argument("--task-kind", choices=sorted(VALID_TASK_KINDS), default="")
    parser.add_argument("--repo-path", default="")
    parser.add_argument("--work-branch", default="")
    parser.add_argument("--executor-provider", default=DEFAULT_EXECUTOR_PROVIDER)
    parser.add_argument("--reviewer-provider", default=DEFAULT_REVIEWER_PROVIDER)
    parser.add_argument("--chat-id", default="")
    parser.add_argument("--message-id", default="")
    parser.add_argument("--risk-level", choices=sorted(VALID_RISK_LEVELS), default=DEFAULT_RISK_LEVEL)
    parser.add_argument("--side-effect-level", choices=sorted(VALID_SIDE_EFFECT_LEVELS), default=DEFAULT_SIDE_EFFECT_LEVEL)
    parser.add_argument("--attempt", type=int, help="Override dispatch attempt number")
    parser.add_argument("--from-json-file", help="Read payload JSON from file")
    parser.add_argument("--from-stdin", action="store_true", help="Read payload JSON from stdin")
    args = parser.parse_args()

    effective_task_id = _norm_str(args.task_id)
    try:
        payload = _load_payload(args)
        if payload and not isinstance(payload, dict):
            raise ValueError("input payload must be a JSON object")

        if isinstance(payload, dict) and payload:
            effective_task_id = _norm_str(payload.get("task_id") or effective_task_id)

        if not effective_task_id:
            raise ValueError("task_id is required")

        request_text = _norm_str(payload.get("request_text") or payload.get("text") or args.request_text)
        source = _norm_str(payload.get("source") or args.source)
        correlation_id = _norm_str(payload.get("correlation_id") or args.correlation_id) or None
        task_kind = _norm_str(payload.get("task_kind") or args.task_kind)
        repo_path = _norm_str(payload.get("repo_path") or args.repo_path)
        work_branch = _norm_str(payload.get("work_branch") or payload.get("branch") or args.work_branch)
        executor_provider = _norm_str(payload.get("executor_provider") or args.executor_provider)
        reviewer_provider = _norm_str(payload.get("reviewer_provider") or args.reviewer_provider)
        chat_id = _norm_str(payload.get("chat_id") or args.chat_id)
        message_id = _norm_str(payload.get("message_id") or args.message_id)
        risk_level = _norm_str(payload.get("risk_level") or args.risk_level)
        side_effect_level = _norm_str(payload.get("side_effect_level") or args.side_effect_level)

        effective_attempt: int | None = args.attempt
        if isinstance(payload, dict) and payload.get("attempt") is not None:
            try:
                effective_attempt = int(payload["attempt"])
            except Exception as exc:
                raise ValueError("attempt must be integer") from exc

        criteria_json = args.criteria_json
        criterion_items = list(args.criterion)
        if isinstance(payload, dict) and payload.get("acceptance_criteria") is not None:
            criteria_json = json.dumps(payload["acceptance_criteria"], ensure_ascii=True)
            criterion_items = []
        elif isinstance(payload, dict) and payload.get("criteria") is not None:
            criteria_json = json.dumps(payload["criteria"], ensure_ascii=True)
            criterion_items = []

        result = run(
            task_id=effective_task_id,
            request_text=request_text,
            source=source,
            correlation_id=correlation_id,
            criteria_json=criteria_json,
            criterion_items=criterion_items,
            task_kind=task_kind,
            repo_path=repo_path,
            work_branch=work_branch,
            executor_provider=executor_provider,
            reviewer_provider=reviewer_provider,
            chat_id=chat_id,
            message_id=message_id,
            risk_level=risk_level,
            side_effect_level=side_effect_level,
            attempt=effective_attempt,
        )
    except Exception as exc:
        append_audit(
            action="coordinator_ingest_failed",
            task_id=effective_task_id or None,
            route="/agn/coordinator/ingest",
            status=500,
            error=type(exc).__name__,
        )
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True))
        return 1

    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
