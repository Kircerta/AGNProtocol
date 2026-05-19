#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))
if str(ROOT / "src") not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT / "scripts"))

from action_protocol import validate_action_payload
from agent_runner import exec_log_path, load_json, run_command, run_reviewer_claude
from event_sourcing import (
    append_event,
    enqueue_action,
    list_pending_actions,
    move_action_file,
    resolve_repo_ref,
    write_perf_summary,
)
from agn.core.guarded_io import atomic_write_text, write_text
from pointer_protocol import ref_to_artifact_entry, read_ref_text, write_text_artifact
try:
    from agn_refs import build_object_ref, parse_object_ref
except ImportError:  # pragma: no cover - package import fallback
    try:
        from scripts.agn_refs import build_object_ref, parse_object_ref
    except ImportError:
        raise

READ_MAX_BYTES = max(512, int((os.environ.get("AGN_READ_ACTION_MAX_BYTES", "65536") or "65536")))


@dataclass
class ActionExecutionResult:
    rc: int
    error_class: str
    result_ref: str
    detail: dict[str, Any]


@contextlib.contextmanager
def _role_env(role: str):
    old_role = os.environ.get("AGN_ROLE")
    old_context = os.environ.get("AGN_RUNTIME_CONTEXT")
    old_enforce = os.environ.get("AGN_ENFORCE_ROLE_GUARD")
    os.environ["AGN_ROLE"] = role
    os.environ["AGN_RUNTIME_CONTEXT"] = "agn_network"
    os.environ["AGN_ENFORCE_ROLE_GUARD"] = "1"
    try:
        yield
    finally:
        if old_role is None:
            os.environ.pop("AGN_ROLE", None)
        else:
            os.environ["AGN_ROLE"] = old_role
        if old_context is None:
            os.environ.pop("AGN_RUNTIME_CONTEXT", None)
        else:
            os.environ["AGN_RUNTIME_CONTEXT"] = old_context
        if old_enforce is None:
            os.environ.pop("AGN_ENFORCE_ROLE_GUARD", None)
        else:
            os.environ["AGN_ENFORCE_ROLE_GUARD"] = old_enforce


def _resolve_cwd(refs: dict[str, Any]) -> Path:
    repo_ref = str(refs.get("repo_ref") or refs.get("repo") or "").strip()
    if repo_ref:
        return resolve_repo_ref(repo_ref)
    # Legacy compatibility path for older queued actions.
    raw = str(refs.get("repo_path") or refs.get("cwd") or "").strip()
    if not raw:
        return ROOT
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = (ROOT / candidate).resolve()
    return candidate


def _sha256_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _truncate_by_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    encoded = str(text).encode("utf-8")
    if len(encoded) <= max_bytes:
        return str(text), False
    clipped = encoded[: max(1, int(max_bytes))].decode("utf-8", errors="ignore")
    return clipped, True


def _make_summary(text: str, max_chars: int = 480) -> str:
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    joined = " ".join(lines[:6]) if lines else str(text).strip()
    clean = joined.replace("\t", " ").replace("\r", " ")
    if len(clean) <= max_chars:
        return clean
    return clean[: max_chars - 24] + "...<truncated-summary>..."


def _read_rejected(
    *,
    trace_id: str,
    task_id: str,
    action_id: str,
    reason: str,
    detail: dict[str, Any],
) -> ActionExecutionResult:
    append_event(
        trace_id=trace_id,
        task_id=task_id,
        event_type="READ_REJECTED",
        action_id=action_id,
        payload={"reason": reason, **detail},
        severity="warn",
    )
    return ActionExecutionResult(
        rc=2,
        error_class="READ_REJECTED",
        result_ref="",
        detail={"reason": reason, **detail},
    )


def _write_read_result_artifacts(
    *,
    task_id: str,
    attempt: int,
    action_id: str,
    text: str,
    max_bytes: int,
    need_summary: bool,
    need_excerpt: bool,
) -> tuple[dict[str, Any], str]:
    excerpt, truncated = _truncate_by_bytes(text, max_bytes=max_bytes)
    summary_text = _make_summary(text)

    summary_ref = None
    excerpt_ref = None
    if need_summary:
        summary_ref = write_text_artifact(
            task_id=task_id,
            attempt=attempt,
            artifact_id=f"{action_id}_read_summary",
            content=summary_text,
            media_type="text/plain",
            filename=f"{action_id}_read_summary.txt",
            source="action_runner",
        )
    if need_excerpt:
        excerpt_ref = write_text_artifact(
            task_id=task_id,
            attempt=attempt,
            artifact_id=f"{action_id}_read_excerpt",
            content=excerpt,
            media_type="text/plain",
            filename=f"{action_id}_read_excerpt.txt",
            source="action_runner",
        )
    result_ref = summary_ref.ref if summary_ref is not None else (excerpt_ref.ref if excerpt_ref is not None else "")
    detail = {
        "sha256": _sha256_text(text),
        "truncated": bool(truncated),
        "source_bytes": len(str(text).encode("utf-8")),
        "max_bytes": int(max_bytes),
    }
    if summary_ref is not None:
        detail["summary_ref"] = ref_to_artifact_entry(summary_ref)
    if excerpt_ref is not None:
        detail["excerpt_ref"] = ref_to_artifact_entry(excerpt_ref)
    return detail, result_ref


def _execute_cmd(action: dict[str, Any]) -> ActionExecutionResult:
    inputs = action.get("inputs", {})
    refs = action.get("refs", {})
    task_id = str(action.get("task_id", "")).strip()
    action_id = str(action.get("action_id", "")).strip()
    attempt = int(inputs.get("attempt", 1) or 1)
    execution_role = str(inputs.get("execution_role", "executor")).strip().lower() or "executor"
    if execution_role not in {"executor", "reviewer", "coordinator", "admin"}:
        execution_role = "executor"
    argv = inputs.get("argv")
    if not isinstance(argv, list) or not argv:
        return ActionExecutionResult(
            rc=2,
            error_class="INVALID_ARGS",
            result_ref="",
            detail={"reason": "inputs.argv must be non-empty list[str]"},
        )
    cmd = [str(item) for item in argv]
    timeout_sec = float(inputs.get("timeout_sec", action.get("budget", {}).get("max_time_sec", 300)) or 300.0)
    log_path = exec_log_path("action_runner", f"{task_id}_{action_id}", attempt)
    try:
        cwd = _resolve_cwd(refs)
    except Exception as exc:
        return ActionExecutionResult(
            rc=2,
            error_class="INVALID_REPO_REF",
            result_ref="",
            detail={"reason": f"{type(exc).__name__}:{exc}"},
        )
    with _role_env(execution_role):
        outcome = run_command(
            cmd=cmd,
            cwd=cwd,
            timeout_sec=max(1.0, timeout_sec),
            log_path=log_path,
        )
    stdout_ref = write_text_artifact(
        task_id=task_id,
        attempt=attempt,
        artifact_id=f"{action_id}_stdout",
        content=outcome.stdout or "",
        media_type="text/plain",
        filename=f"{action_id}_stdout.log",
        source="action_runner",
    )
    stderr_ref = write_text_artifact(
        task_id=task_id,
        attempt=attempt,
        artifact_id=f"{action_id}_stderr",
        content=outcome.stderr or "",
        media_type="text/plain",
        filename=f"{action_id}_stderr.log",
        source="action_runner",
    )
    blocked_reason = ""
    error_class = ""
    if outcome.return_code != 0:
        if outcome.return_code == 126 and "ROLE_GUARD_BLOCKED:" in (outcome.stderr or ""):
            error_class = "ROLE_GUARD_BLOCKED"
            blocked_reason = str(outcome.stderr or "").split("ROLE_GUARD_BLOCKED:", 1)[-1].strip().splitlines()[0]
        elif outcome.timed_out:
            error_class = "TIMEOUT"
        else:
            error_class = "NONZERO_EXIT"
    return ActionExecutionResult(
        rc=int(outcome.return_code),
        error_class=error_class,
        result_ref=stdout_ref.ref,
        detail={
            "execution_role": execution_role,
            "stdout_ref": ref_to_artifact_entry(stdout_ref),
            "stderr_ref": ref_to_artifact_entry(stderr_ref),
            "timed_out": bool(outcome.timed_out),
            "duration_ms": round(outcome.duration_ms, 2),
            "blocked_reason": blocked_reason,
        },
    )


def _execute_write_file(action: dict[str, Any]) -> ActionExecutionResult:
    inputs = action.get("inputs", {})
    refs = action.get("refs", {})
    task_id = str(action.get("task_id", "")).strip()
    attempt = int(inputs.get("attempt", 1) or 1)
    target_raw = str(refs.get("target_path") or inputs.get("target_path") or "").strip()
    if not target_raw:
        return ActionExecutionResult(
            rc=2,
            error_class="INVALID_ARGS",
            result_ref="",
            detail={"reason": "target_path is required"},
        )
    target = Path(target_raw)
    if not target.is_absolute():
        target = (ROOT / target).resolve()

    content = str(inputs.get("content", ""))
    content_ref = str(refs.get("content_ref", "")).strip()
    if content_ref:
        content = read_ref_text(content_ref, mode="all", max_bytes=512 * 1024)
    with _role_env("executor"):
        write_text(target, content)
    file_ref = write_text_artifact(
        task_id=task_id,
        attempt=attempt,
        artifact_id=f"{action.get('action_id', 'write')}_written_file",
        content=f"path={target}\nbytes={len(content.encode('utf-8'))}\n",
        media_type="text/plain",
        filename=f"{action.get('action_id', 'write')}_write_receipt.txt",
        source="action_runner",
    )
    return ActionExecutionResult(
        rc=0,
        error_class="",
        result_ref=file_ref.ref,
        detail={
            "target_ref": str(refs.get("target_ref", "")).strip(),
            "bytes": len(content.encode("utf-8")),
            "receipt_ref": ref_to_artifact_entry(file_ref),
        },
    )


def _execute_read_ref(action: dict[str, Any]) -> ActionExecutionResult:
    inputs = action.get("inputs", {})
    refs = action.get("refs", {})
    trace_id = str(action.get("trace_id", "")).strip()
    task_id = str(action.get("task_id", "")).strip()
    action_id = str(action.get("action_id", "")).strip()
    attempt = int(inputs.get("attempt", 1) or 1)

    target_ref = str(refs.get("ref") or inputs.get("ref") or "").strip()
    if not target_ref:
        return _read_rejected(
            trace_id=trace_id,
            task_id=task_id,
            action_id=action_id,
            reason="missing_ref",
            detail={},
        )
    if not target_ref.startswith("agn://"):
        return _read_rejected(
            trace_id=trace_id,
            task_id=task_id,
            action_id=action_id,
            reason="invalid_ref",
            detail={"ref": target_ref},
        )

    budget = action.get("budget", {}) if isinstance(action.get("budget"), dict) else {}
    budget_cap = max(128, int(float(budget.get("max_log_kb", 64) or 64) * 1024))
    requested = max(128, int(inputs.get("max_bytes", budget_cap) or budget_cap))
    max_bytes = min(READ_MAX_BYTES, min(requested, budget_cap))
    need_summary = bool(inputs.get("need_summary", True))
    need_excerpt = bool(inputs.get("need_excerpt", True))
    try:
        content = read_ref_text(target_ref, mode="all", max_bytes=READ_MAX_BYTES)
    except Exception as exc:
        return _read_rejected(
            trace_id=trace_id,
            task_id=task_id,
            action_id=action_id,
            reason="ref_read_failed",
            detail={"ref": target_ref, "error": f"{type(exc).__name__}:{exc}"},
        )

    detail, result_ref = _write_read_result_artifacts(
        task_id=task_id,
        attempt=attempt,
        action_id=action_id,
        text=content,
        max_bytes=max_bytes,
        need_summary=need_summary,
        need_excerpt=need_excerpt,
    )
    append_event(
        trace_id=trace_id,
        task_id=task_id,
        event_type="READ_RESULT_CREATED",
        action_id=action_id,
        payload={"read_type": "READ_REF", "source_ref": target_ref, **detail},
    )
    return ActionExecutionResult(rc=0, error_class="", result_ref=result_ref, detail=detail)


def _resolve_repo_target(repo_root: Path, raw_path: str) -> Path:
    candidate = Path(str(raw_path).strip())
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (repo_root / candidate).resolve()
    repo_resolved = repo_root.resolve()
    if repo_resolved not in resolved.parents and resolved != repo_resolved:
        raise ValueError("path_outside_repo")
    return resolved


def _extract_by_line_range(text: str, line_range: dict[str, Any]) -> str:
    start = max(1, int(line_range.get("start", 1) or 1))
    end = max(start, int(line_range.get("end", start + 199) or (start + 199)))
    lines = text.splitlines()
    return "\n".join(lines[start - 1 : end])


def _extract_by_byte_range(data: bytes, byte_range: dict[str, Any]) -> bytes:
    start = max(0, int(byte_range.get("start", 0) or 0))
    end = max(start + 1, int(byte_range.get("end", start + 4096) or (start + 4096)))
    return data[start:end]


def _execute_read_repo_file(action: dict[str, Any]) -> ActionExecutionResult:
    inputs = action.get("inputs", {})
    refs = action.get("refs", {})
    trace_id = str(action.get("trace_id", "")).strip()
    task_id = str(action.get("task_id", "")).strip()
    action_id = str(action.get("action_id", "")).strip()
    attempt = int(inputs.get("attempt", 1) or 1)

    repo_ref = str(refs.get("repo_ref") or "").strip()
    try:
        repo_root = _resolve_cwd(refs)
    except Exception as exc:
        return _read_rejected(
            trace_id=trace_id,
            task_id=task_id,
            action_id=action_id,
            reason="invalid_repo_ref",
            detail={"repo_ref": repo_ref or "agn://repo/main", "error": f"{type(exc).__name__}:{exc}"},
        )
    raw_path = str(inputs.get("path") or refs.get("path") or "").strip()
    if not raw_path:
        return _read_rejected(
            trace_id=trace_id,
            task_id=task_id,
            action_id=action_id,
            reason="missing_path",
            detail={},
        )
    if not repo_root.exists() or not repo_root.is_dir():
        return _read_rejected(
            trace_id=trace_id,
            task_id=task_id,
            action_id=action_id,
            reason="repo_not_found",
            detail={"repo_ref": repo_ref or "agn://repo/main"},
        )
    try:
        target = _resolve_repo_target(repo_root, raw_path)
    except Exception as exc:
        return _read_rejected(
            trace_id=trace_id,
            task_id=task_id,
            action_id=action_id,
            reason="path_forbidden",
            detail={"repo_ref": repo_ref or "agn://repo/main", "path": raw_path, "error": f"{type(exc).__name__}:{exc}"},
        )
    if not target.exists() or not target.is_file():
        return _read_rejected(
            trace_id=trace_id,
            task_id=task_id,
            action_id=action_id,
            reason="file_not_found",
            detail={"path": raw_path},
        )

    max_bytes = min(READ_MAX_BYTES, max(128, int(inputs.get("max_bytes", 4096) or 4096)))
    need_summary = bool(inputs.get("need_summary", True))
    need_excerpt = bool(inputs.get("need_excerpt", True))
    raw = target.read_bytes()
    selected = raw
    byte_range = inputs.get("byte_range")
    line_range = inputs.get("line_range")
    if isinstance(byte_range, dict):
        selected = _extract_by_byte_range(raw, byte_range)
    elif isinstance(line_range, dict):
        text = raw.decode("utf-8", errors="replace")
        selected = _extract_by_line_range(text, line_range).encode("utf-8")
    content = selected.decode("utf-8", errors="replace")

    detail, result_ref = _write_read_result_artifacts(
        task_id=task_id,
        attempt=attempt,
        action_id=action_id,
        text=content,
        max_bytes=max_bytes,
        need_summary=need_summary,
        need_excerpt=need_excerpt,
    )
    append_event(
        trace_id=trace_id,
        task_id=task_id,
        event_type="READ_RESULT_CREATED",
        action_id=action_id,
        payload={
            "read_type": "READ_REPO_FILE",
            "repo_ref": repo_ref or "agn://repo/main",
            "path": raw_path,
            **detail,
        },
    )
    return ActionExecutionResult(rc=0, error_class="", result_ref=result_ref, detail=detail)


def _resolve_object_or_path_ref(*, kind: str, ref: str, task_id: str, default_attempt: int) -> tuple[Path, int]:
    clean = str(ref or "").strip()
    if clean.startswith("agn://object/"):
        parsed = parse_object_ref(clean)
        obj_kind = str(parsed.get("kind", ""))
        attempt = int(parsed.get("attempt", default_attempt) or default_attempt)
        if obj_kind != kind:
            raise ValueError(f"object_ref_kind_mismatch:{obj_kind}!={kind}")
        if kind == "dispatch":
            return (ROOT / "dispatch" / f"{task_id}.json").resolve(), attempt
        if kind == "result":
            return (ROOT / "results" / f"{task_id}.{attempt}.json").resolve(), attempt
        if kind == "verdict":
            return (ROOT / "verdicts" / f"{task_id}.{attempt}.md").resolve(), attempt
        raise ValueError(f"unsupported_object_kind:{kind}")

    # Legacy compatibility: allow path refs already queued.
    path = Path(clean)
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    return path, max(1, int(default_attempt))


def _execute_request_review(action: dict[str, Any]) -> ActionExecutionResult:
    refs = action.get("refs", {})
    inputs = action.get("inputs", {})
    trace_id = str(action.get("trace_id", "")).strip()
    task_id = str(action.get("task_id", "")).strip()

    dispatch_ref = str(refs.get("dispatch_ref", "")).strip()
    result_ref = str(refs.get("result_ref", "")).strip()
    # Legacy fallback for actions enqueued before Evo4.
    if not dispatch_ref and str(refs.get("dispatch_path", "")).strip():
        dispatch_ref = str(refs.get("dispatch_path", "")).strip()
    if not result_ref and str(refs.get("result_path", "")).strip():
        result_ref = str(refs.get("result_path", "")).strip()

    if not dispatch_ref or not result_ref:
        return ActionExecutionResult(
            rc=2,
            error_class="INVALID_ARGS",
            result_ref="",
            detail={"reason": "dispatch_ref and result_ref are required"},
        )

    attempt = int(inputs.get("attempt", 1) or 1)
    try:
        dispatch_path, _ = _resolve_object_or_path_ref(kind="dispatch", ref=dispatch_ref, task_id=task_id, default_attempt=attempt)
        result_path, attempt = _resolve_object_or_path_ref(kind="result", ref=result_ref, task_id=task_id, default_attempt=attempt)
    except Exception as exc:
        return ActionExecutionResult(
            rc=2,
            error_class="INVALID_ARGS",
            result_ref="",
            detail={"reason": f"{type(exc).__name__}:{exc}"},
        )
    if not dispatch_path.exists() or not result_path.exists():
        return ActionExecutionResult(
            rc=2,
            error_class="MISSING_INPUT",
            result_ref="",
            detail={"dispatch_ref": dispatch_ref, "result_ref": result_ref},
        )

    dispatch = load_json(dispatch_path)
    result_payload = load_json(result_path)
    with _role_env("reviewer"):
        rc, verdict_target = run_reviewer_claude(dispatch, result_payload)
    verdict_ref = str(refs.get("verdict_ref", "")).strip() or build_object_ref("verdict", trace_id or task_id, attempt)
    receipt = write_text_artifact(
        task_id=task_id,
        attempt=attempt,
        artifact_id=f"{action.get('action_id', 'review')}_verdict_receipt",
        content=f"verdict_ref={verdict_ref}\nrc={rc}\n",
        media_type="text/plain",
        filename=f"{action.get('action_id', 'review')}_verdict_receipt.txt",
        source="action_runner",
    )
    return ActionExecutionResult(
        rc=rc,
        error_class="" if rc == 0 else "REVIEW_FAILED",
        result_ref=receipt.ref,
        detail={"verdict_ref": verdict_ref, "receipt_ref": ref_to_artifact_entry(receipt)},
    )


def _execute_summarize(action: dict[str, Any]) -> ActionExecutionResult:
    inputs = action.get("inputs", {})
    refs = action.get("refs", {})
    task_id = str(action.get("task_id", "")).strip()
    attempt = int(inputs.get("attempt", 1) or 1)
    parts: list[str] = []
    content = str(inputs.get("content", "")).strip()
    if content:
        parts.append(content[:2000])
    source_refs = refs.get("source_refs")
    if isinstance(source_refs, list):
        for ref in source_refs[:5]:
            try:
                parts.append(read_ref_text(str(ref), mode="tail", tail_lines=20, max_bytes=4096))
            except Exception:
                continue
    summary = "\n\n".join(parts)[:4000]
    summary_ref = write_text_artifact(
        task_id=task_id,
        attempt=attempt,
        artifact_id=f"{action.get('action_id', 'summary')}_summary",
        content=summary,
        media_type="text/plain",
        filename=f"{action.get('action_id', 'summary')}_summary.txt",
        source="action_runner",
    )
    return ActionExecutionResult(
        rc=0,
        error_class="",
        result_ref=summary_ref.ref,
        detail={"summary_ref": ref_to_artifact_entry(summary_ref)},
    )


def _execute_noop(action: dict[str, Any], error_class: str = "") -> ActionExecutionResult:
    return ActionExecutionResult(
        rc=0,
        error_class=error_class,
        result_ref="",
        detail={"note": f"noop action: {action.get('action_type', '')}"},
    )


def execute_action(action: dict[str, Any]) -> ActionExecutionResult:
    action_type = str(action.get("action_type", "")).strip()
    if action_type == "EXECUTE_CMD":
        return _execute_cmd(action)
    if action_type == "WRITE_FILE":
        return _execute_write_file(action)
    if action_type == "READ_REF":
        return _execute_read_ref(action)
    if action_type == "READ_REPO_FILE":
        return _execute_read_repo_file(action)
    if action_type == "REQUEST_REVIEW":
        return _execute_request_review(action)
    if action_type == "SUMMARIZE":
        return _execute_summarize(action)
    if action_type == "RETRY":
        return _execute_noop(action)
    if action_type == "ABORT":
        return _execute_noop(action, error_class="ABORTED")
    return ActionExecutionResult(
        rc=2,
        error_class="UNKNOWN_ACTION",
        result_ref="",
        detail={"reason": f"unsupported action_type: {action_type}"},
    )


def _check_budget(action: dict[str, Any], *, started: float, result: ActionExecutionResult) -> dict[str, Any]:
    budget = action.get("budget", {})
    exceeded: list[str] = []
    elapsed_sec = max(0.0, time.time() - started)
    max_time = float(budget.get("max_time_sec", 0.0) or 0.0)
    if max_time > 0 and elapsed_sec > max_time:
        exceeded.append("max_time_sec")
    log_bytes = len(json.dumps(result.detail, ensure_ascii=True).encode("utf-8"))
    max_log_kb = float(budget.get("max_log_kb", 0.0) or 0.0)
    if max_log_kb > 0 and log_bytes > max_log_kb * 1024:
        exceeded.append("max_log_kb")
    max_disk_mb = float(budget.get("max_disk_mb", 0.0) or 0.0)
    if max_disk_mb > 0 and result.detail.get("stdout_ref", {}).get("bytes", 0) > max_disk_mb * 1024 * 1024:
        exceeded.append("max_disk_mb")
    return {
        "elapsed_sec": round(elapsed_sec, 3),
        "log_bytes": log_bytes,
        "exceeded": exceeded,
    }


def process_action_file(path: Path) -> dict[str, Any]:
    started = time.time()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        payload = {}
        result = {
            "ok": False,
            "error": f"invalid_json:{type(exc).__name__}",
            "action_file": path.name,
        }
        move_action_file(path, status="failed")
        return result

    trace_id = str(payload.get("trace_id", "")).strip() or "unknown-trace"
    task_id = str(payload.get("task_id", "")).strip() or "unknown-task"
    action_id = str(payload.get("action_id", "")).strip() or path.stem

    vr = validate_action_payload(payload)
    if not vr.valid:
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="PROTOCOL_VIOLATION",
            action_id=action_id,
            payload={"errors": vr.errors},
            severity="error",
        )
        move_action_file(path, status="failed")
        return {"ok": False, "trace_id": trace_id, "task_id": task_id, "action_id": action_id, "errors": vr.errors}

    append_event(
        trace_id=trace_id,
        task_id=task_id,
        event_type="ACTION_STARTED",
        action_id=action_id,
        payload={"action_type": payload.get("action_type"), "budget": payload.get("budget", {})},
    )
    result = execute_action(payload)
    budget_info = _check_budget(payload, started=started, result=result)
    if budget_info["exceeded"]:
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="PERF_BUDGET_EXCEEDED",
            action_id=action_id,
            payload={"exceeded": budget_info["exceeded"], "elapsed_sec": budget_info["elapsed_sec"], "log_bytes": budget_info["log_bytes"]},
            severity="warn",
        )

    if result.error_class == "ROLE_GUARD_BLOCKED":
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="ROLE_GUARD_BLOCKED",
            action_id=action_id,
            payload={
                "action_type": payload.get("action_type"),
                "execution_role": str(result.detail.get("execution_role", "")),
                "reason": str(result.detail.get("blocked_reason", "")).strip(),
            },
            severity="warn",
        )

    append_event(
        trace_id=trace_id,
        task_id=task_id,
        event_type="ACTION_FINISHED",
        action_id=action_id,
        payload={
            "action_type": payload.get("action_type"),
            "rc": result.rc,
            "error_class": result.error_class,
            "result_ref": result.result_ref,
            "detail": result.detail,
            "elapsed_sec": budget_info["elapsed_sec"],
        },
        severity="info" if result.rc == 0 else "error",
    )
    move_action_file(path, status="done" if result.rc == 0 else "failed")
    return {
        "ok": result.rc == 0,
        "trace_id": trace_id,
        "task_id": task_id,
        "action_id": action_id,
        "rc": result.rc,
        "error_class": result.error_class,
        "elapsed_sec": budget_info["elapsed_sec"],
    }


def run_pending(*, max_actions: int = 20) -> dict[str, Any]:
    items = list_pending_actions()
    processed = 0
    errors = 0
    action_metrics: list[dict[str, Any]] = []
    started = time.time()
    trace_id = ""
    task_id = ""
    for path in items:
        if processed >= max_actions:
            break
        one = process_action_file(path)
        processed += 1
        trace_id = str(one.get("trace_id", "")).strip() or trace_id
        task_id = str(one.get("task_id", "")).strip() or task_id
        if not one.get("ok", False):
            errors += 1
        action_metrics.append(
            {
                "action_id": one.get("action_id", ""),
                "rc": int(one.get("rc", 1) or 1),
                "duration_ms": float(one.get("elapsed_sec", 0.0) or 0.0) * 1000.0,
            }
        )
    perf_path = ""
    if trace_id and task_id:
        perf = write_perf_summary(trace_id=trace_id, task_id=task_id, started_ts=started, actions=action_metrics, budget={"max_time_sec": 3600, "max_disk_mb": 1024, "max_log_kb": 2048})
        try:
            perf_path = str(perf.resolve().relative_to(ROOT))
        except Exception:
            perf_path = perf.name
    return {
        "processed": processed,
        "errors": errors,
        "pending_left": max(0, len(items) - processed),
        "perf_path": perf_path,
    }


def _seed_action_from_file(json_file: Path) -> dict[str, Any]:
    payload = json.loads(json_file.read_text(encoding="utf-8"))
    enqueue_action(payload)
    return {"ok": True, "enqueued": str(json_file)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run AGN action protocol queue")
    parser.add_argument("--once", action="store_true", help="Process queue once")
    parser.add_argument("--max-actions", type=int, default=20)
    parser.add_argument("--seed-action-json", help="enqueue one action json file then exit")
    args = parser.parse_args()

    if args.seed_action_json:
        result = _seed_action_from_file(Path(args.seed_action_json))
        print(json.dumps(result, ensure_ascii=True))
        return 0

    summary = run_pending(max_actions=max(1, int(args.max_actions)))
    print(json.dumps(summary, ensure_ascii=True))
    return 0 if int(summary.get("errors", 0) or 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
