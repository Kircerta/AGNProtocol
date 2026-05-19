#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runner import load_json, result_path, verdict_path
from coordinator_ingest import run as run_coordinator_ingest
from executor_worker import process_once as run_executor_once
from agn_notify_runtime import enqueue_message
from reviewer_worker import process_once as run_reviewer_once


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.from_json_file and args.from_stdin:
        raise ValueError("use only one of --from-json-file or --from-stdin")

    payload: dict[str, Any]
    if args.from_json_file:
        payload = json.loads(Path(args.from_json_file).read_text(encoding="utf-8"))
    elif args.from_stdin:
        payload = json.load(sys.stdin)
    else:
        raise ValueError("input payload is required (--from-stdin or --from-json-file)")

    if not isinstance(payload, dict):
        raise ValueError("input payload must be a JSON object")
    return payload


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _criteria_inputs(payload: dict[str, Any]) -> tuple[str | None, list[str]]:
    if payload.get("acceptance_criteria") is not None:
        return json.dumps(payload["acceptance_criteria"], ensure_ascii=True), []
    if payload.get("criteria") is not None:
        return json.dumps(payload["criteria"], ensure_ascii=True), []
    return None, []


def _emit_explicit_message(
    *,
    payload: dict[str, Any],
    text: str,
    message_kind: str,
    task_id: str,
    correlation_id: str,
) -> None:
    chat_id = str(payload.get("chat_id") or payload.get("notify_chat_id") or "").strip()
    emit_messages_raw = payload.get("emit_messages")
    emit_messages = True if emit_messages_raw is None else bool(emit_messages_raw)
    if not emit_messages or not chat_id:
        return

    try:
        enqueue_message(
            text=text,
            chat_id=chat_id,
            task_id=task_id,
            correlation_id=correlation_id,
            message_kind=message_kind,
            source="run_agn_task",
        )
    except Exception:
        # Never break contract output because message queueing fails.
        return


def _resolve_worker_role_binding(payload: dict[str, Any]) -> dict[str, str]:
    defaults = {"execute": "executor", "review": "reviewer"}
    raw = payload.get("role_binding")
    if str(os.environ.get("AGN_ALLOW_CUSTOM_ROLE_BINDING", "")).strip() != "1":
        return defaults
    if not isinstance(raw, dict):
        return defaults

    allowed = {"executor", "reviewer"}
    if str(os.environ.get("AGN_ALLOW_ADMIN_ROLE_BINDING", "")).strip() == "1":
        allowed.add("admin")

    execute = str(raw.get("execute", defaults["execute"])).strip().lower()
    review = str(raw.get("review", defaults["review"])).strip().lower()
    if execute not in allowed:
        execute = defaults["execute"]
    if review not in allowed:
        review = defaults["review"]
    return {"execute": execute, "review": review}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run AGN one-shot task lifecycle")
    parser.add_argument("--from-json-file", help="Read task payload JSON from file")
    parser.add_argument("--from-stdin", action="store_true", help="Read task payload JSON from stdin")
    args = parser.parse_args()

    try:
        payload = _load_payload(args)

        task_id = str(payload.get("task_id") or "").strip()
        if not task_id:
            raise ValueError("task_id is required")

        criteria_json, criterion_items = _criteria_inputs(payload)
        ingest_result = run_coordinator_ingest(
            task_id=task_id,
            request_text=str(payload.get("request_text") or payload.get("text") or "").strip() or "agn task",
            source=str(payload.get("source") or "openclaw").strip() or "openclaw",
            correlation_id=str(payload.get("correlation_id") or "").strip() or None,
            criteria_json=criteria_json,
            criterion_items=criterion_items,
            task_kind=str(payload.get("task_kind") or "").strip(),
            repo_path=str(payload.get("repo_path") or "").strip(),
            work_branch=str(payload.get("work_branch") or payload.get("branch") or "").strip(),
            executor_provider=str(payload.get("executor_provider") or "codex").strip(),
            reviewer_provider=str(payload.get("reviewer_provider") or "gemini").strip(),
            chat_id=str(payload.get("chat_id") or "").strip(),
            message_id=str(payload.get("message_id") or "").strip(),
            risk_level=str(payload.get("risk_level") or "low").strip(),
            side_effect_level=str(payload.get("side_effect_level") or "read_only").strip(),
            attempt=_int_or_none(payload.get("attempt")),
        )

        task_id = str(ingest_result.get("task_id") or task_id)
        attempt = int(ingest_result.get("attempt") or 1)
        correlation_id = str(ingest_result.get("correlation_id") or payload.get("correlation_id") or "").strip()
        _emit_explicit_message(
            payload=payload,
            text=f"[AGN] Task accepted. task_id={task_id}, attempt={attempt}",
            message_kind="progress",
            task_id=task_id,
            correlation_id=correlation_id,
        )

        # P2-1: Role binding — set AGN_ROLE for each worker phase.
        role_binding = _resolve_worker_role_binding(payload)
        _original_role = os.environ.get("AGN_ROLE", "")
        _original_context = os.environ.get("AGN_RUNTIME_CONTEXT", "")
        _original_enforce = os.environ.get("AGN_ENFORCE_ROLE_GUARD", "")

        # P3-19: capture stdout from worker calls and log it instead of discarding.
        buffered_stdout = io.StringIO()
        with contextlib.redirect_stdout(buffered_stdout):
            os.environ["AGN_RUNTIME_CONTEXT"] = "agn_network"
            os.environ["AGN_ENFORCE_ROLE_GUARD"] = "1"
            try:
                os.environ["AGN_ROLE"] = str(role_binding.get("execute", "executor"))
                executor_summary = run_executor_once(max_per_tick=1, mode="real", task_filter=task_id)
                os.environ["AGN_ROLE"] = str(role_binding.get("review", "reviewer"))
                reviewer_summary = run_reviewer_once(max_per_tick=1, mode="real", task_filter=task_id)
            finally:
                if _original_role:
                    os.environ["AGN_ROLE"] = _original_role
                else:
                    os.environ.pop("AGN_ROLE", None)
                if _original_context:
                    os.environ["AGN_RUNTIME_CONTEXT"] = _original_context
                else:
                    os.environ.pop("AGN_RUNTIME_CONTEXT", None)
                if _original_enforce:
                    os.environ["AGN_ENFORCE_ROLE_GUARD"] = _original_enforce
                else:
                    os.environ.pop("AGN_ENFORCE_ROLE_GUARD", None)
        captured = buffered_stdout.getvalue()
        if captured.strip():
            logging.getLogger("run_agn_task").info("worker stdout:\n%s", captured.rstrip())

        result_file = result_path(task_id, attempt)
        verdict_file = verdict_path(task_id, attempt)

        fail_reasons: list[str] = []
        if int(executor_summary.get("errors", 0) or 0) > 0:
            fail_reasons.append("executor_worker_reported_errors")
        if int(reviewer_summary.get("errors", 0) or 0) > 0:
            fail_reasons.append("reviewer_worker_reported_errors")

        result_payload: dict[str, Any] = {}
        verdict_payload: dict[str, Any] = {}

        if result_file.exists():
            result_payload = load_json(result_file)
            for reason in result_payload.get("fail_reasons", []) or []:
                fail_reasons.append(str(reason))
        else:
            fail_reasons.append("result_not_found")

        if verdict_file.exists():
            verdict_payload = load_json(verdict_file)
            for reason in verdict_payload.get("fail_reasons", []) or []:
                fail_reasons.append(str(reason))
        else:
            fail_reasons.append("verdict_not_found")

        decision = str(verdict_payload.get("decision") or "").strip()
        commit_hash = str(result_payload.get("commit_hash") or "").strip() or None
        no_change_reason = str(result_payload.get("no_change_reason") or "").strip() or None

        response = {
            "ok": len(fail_reasons) == 0 and bool(decision),
            "task_id": task_id,
            "attempt": attempt,
            "decision": decision,
            "commit_hash": commit_hash,
            "no_change_reason": no_change_reason,
            "result_path": str(result_file),
            "verdict_path": str(verdict_file),
            "fail_reasons": sorted(set(fail_reasons)),
        }
        if response["ok"]:
            _emit_explicit_message(
                payload=payload,
                text=f"[AGN] Task complete. task_id={task_id}, decision={decision}",
                message_kind="progress",
                task_id=task_id,
                correlation_id=correlation_id,
            )
        else:
            _emit_explicit_message(
                payload=payload,
                text=f"[AGN] Task failed. task_id={task_id}, reasons={','.join(response['fail_reasons'])}",
                message_kind="alert",
                task_id=task_id,
                correlation_id=correlation_id,
            )
        print(json.dumps(response, ensure_ascii=True))
        return 0 if response["ok"] else 1

    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "task_id": str(payload.get("task_id") if "payload" in locals() and isinstance(payload, dict) else ""),
                    "attempt": None,
                    "decision": "",
                    "commit_hash": None,
                    "no_change_reason": None,
                    "result_path": "",
                    "verdict_path": "",
                    "fail_reasons": [str(exc)],
                },
                ensure_ascii=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
