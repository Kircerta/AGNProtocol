#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agn_api.ssot_store import SSOTStore
from agent_runner import (
    PATHS,
    append_audit,
    load_json,
    list_dispatches,
    run_reviewer_claude,
    result_path,
    run_loop,
    utc_now_iso,
    verdict_path,
)
from validation.fake_reviewer import run as run_reviewer

LOCK_THRESHOLD = max(1, int(os.getenv("AGN_HALLUCINATION_LOCK_THRESHOLD", "3") or "3"))
# Per-task timeout for the reviewer worker (default 900s = 15 min).
_REVIEWER_TASK_TIMEOUT = max(60, int(os.getenv("AGN_REVIEWER_TASK_TIMEOUT", "900") or "900"))


def _dispatch_task_kind(dispatch: dict[str, object]) -> str:
    raw = str(dispatch.get("task_kind", "")).strip().lower()
    if raw in {"repo", "protocol"}:
        return raw
    return "protocol"


def _is_infrastructure_failure(verdict_payload: dict[str, object] | None, *, verdict_file_exists: bool = False) -> bool:
    """Return True if the verdict indicates an infrastructure/provider failure
    not a genuine content-based reject. Infrastructure failures should
    not count toward the hallucination lock threshold.

    P2-BUG-FIX: When the verdict file exists but could not be loaded
    (verdict_payload is None), this is a file-system / partial-write issue
    — an infrastructure failure, not a content reject.
    When the verdict file does NOT exist and verdict_payload is None, the
    reviewer crashed before writing anything — also infrastructure.
    """
    if not isinstance(verdict_payload, dict):
        # P2-BUG-FIX: None verdict with existing file = partial-write race.
        # None verdict without file = reviewer crash.  Both are infrastructure.
        return True
    fail_reasons = verdict_payload.get("fail_reasons", [])
    if not isinstance(fail_reasons, list):
        return False
    infra_prefixes = ("reviewer_unavailable:", "failed to parse reviewer output")
    return any(
        isinstance(r, str) and any(r.startswith(p) for p in infra_prefixes)
        for r in fail_reasons
    )


def _update_hallucination_state(*, task_id: str, verdict_file_exists: bool, verdict_payload: dict[str, object] | None, reviewer_rc: int) -> None:
    store = SSOTStore(PATHS.ssot_dir)

    # Use locked_update to prevent concurrent read-modify-write races (P1-4).
    with store.locked_update(task_id) as task:
        if not isinstance(task, dict):
            return

        qa_retry_count = int(task.get("qa_retry_count", 0) or 0)
        lock_state = str(task.get("lock_state", "active")).strip().lower() or "active"

        decision = ""
        if verdict_file_exists and isinstance(verdict_payload, dict):
            decision = str(verdict_payload.get("decision", "")).strip().lower()

        # P0-1 fix: propagate verdict decision back to SSOT so derive_status()
        # can transition the task to approved/rejected.
        if decision == "approve":
            task["decision"] = "approved"
            task["reviewed_at"] = utc_now_iso()
        elif decision == "reject":
            task["decision"] = "rejected"
            task["reviewed_at"] = utc_now_iso()

        # P1-7: infrastructure failures (provider unavailable, parse errors)
        # should NOT increment qa_retry_count.  Only genuine content-based
        # rejects count toward the hallucination lock.
        infra_failure = _is_infrastructure_failure(verdict_payload, verdict_file_exists=verdict_file_exists)
        failed_or_rejected = reviewer_rc != 0 or decision == "reject"
        if failed_or_rejected and not infra_failure:
            qa_retry_count += 1
        elif not failed_or_rejected and lock_state != "halted":
            qa_retry_count = 0

        task["qa_retry_count"] = qa_retry_count

        if lock_state != "halted":
            if qa_retry_count >= LOCK_THRESHOLD:
                task["lock_state"] = "halted"
                task["lock_reason"] = f"qa_retry_count_threshold_reached:{LOCK_THRESHOLD}"
                task["locked_at"] = utc_now_iso()
                task.pop("lock_notified_at", None)
            else:
                task["lock_state"] = "active"
                if qa_retry_count == 0:
                    task["lock_reason"] = ""
                    task["locked_at"] = ""


def process_once(max_per_tick: int, *, mode: str, task_filter: str | None) -> dict[str, int]:
    processed = 0
    skipped = 0
    errors = 0

    for _, dispatch in list_dispatches():
        if processed >= max_per_tick:
            break

        task_id = str(dispatch.get("task_id", "")).strip()
        attempt = int(dispatch.get("attempt", 0) or 0)
        if not task_id or attempt <= 0:
            skipped += 1
            continue
        if task_filter and task_id != task_filter:
            skipped += 1
            continue

        result_file = result_path(task_id, attempt)
        if not result_file.exists():
            skipped += 1
            continue

        verdict_file = verdict_path(task_id, attempt)
        if verdict_file.exists():
            skipped += 1
            continue

        task_kind = _dispatch_task_kind(dispatch)
        use_real_reviewer = mode == "real" and task_kind == "repo"
        verdict_payload: dict[str, object] | None = None

        # P2-13: worker-level timeout enforcement.
        # Uses a monotonic deadline to avoid threading.Timer race conditions.
        task_timed_out = False
        deadline = time.monotonic() + _REVIEWER_TASK_TIMEOUT
        try:
            if use_real_reviewer:
                result_payload = load_json(result_file)
                code, _ = run_reviewer_claude(dispatch, result_payload)
            else:
                code = run_reviewer(task_id=task_id)
        except Exception:
            code = 1
        if time.monotonic() > deadline:
            task_timed_out = True

        # P2-BUG-FIX: Only apply timeout code when task did NOT succeed.
        # Same fix as executor_worker — a successful review should not be
        # overwritten with timeout code 124.
        if task_timed_out and code != 0:
            code = 124
            append_audit(
                action="reviewer_task_timeout",
                task_id=task_id,
                route="/agn/reviewer",
                status=504,
                attempt=attempt,
                timeout_sec=_REVIEWER_TASK_TIMEOUT,
            )

        if code == 0:
            processed += 1
            if not use_real_reviewer:
                append_audit(
                    action="reviewer_processed",
                    task_id=task_id,
                    route="/agn/reviewer",
                    status=200,
                    attempt=attempt,
                    task_kind=task_kind,
                    worker_mode=mode,
                )
        else:
            errors += 1
            if not use_real_reviewer:
                append_audit(
                    action="reviewer_failed",
                    task_id=task_id,
                    route="/agn/reviewer",
                    status=500,
                    attempt=attempt,
                    rc=code,
                    task_kind=task_kind,
                    worker_mode=mode,
                )

        verdict_file_found = verdict_file.exists()
        if verdict_file_found:
            # P2-BUG-FIX: retry once after brief pause to handle partial-write
            # race where the reviewer process is still flushing/renaming.
            for _attempt in range(2):
                try:
                    loaded_verdict = load_json(verdict_file)
                    if isinstance(loaded_verdict, dict):
                        verdict_payload = loaded_verdict
                        break
                except Exception:
                    verdict_payload = None
                    if _attempt == 0:
                        time.sleep(0.1)

        _update_hallucination_state(
            task_id=task_id,
            verdict_file_exists=verdict_file_found,
            verdict_payload=verdict_payload,
            reviewer_rc=code,
        )

    return {"processed": processed, "skipped": skipped, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description="Reviewer worker processing result files")
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-per-tick", type=int, default=20)
    parser.add_argument("--mode", choices=["real", "fake"], default="real")
    parser.add_argument("--task-id", help="Process only one task_id")
    args = parser.parse_args()

    os.environ.setdefault("AGN_ROLE", "reviewer")
    os.environ.setdefault("AGN_RUNTIME_CONTEXT", "agn_network")
    os.environ.setdefault("AGN_ENFORCE_ROLE_GUARD", "1")

    max_per_tick = max(1, args.max_per_tick)
    return run_loop(
        worker_name="reviewer",
        interval_seconds=max(0.1, args.interval_seconds),
        once=args.once,
        handler=lambda: process_once(max_per_tick=max_per_tick, mode=args.mode, task_filter=args.task_id),
    )


if __name__ == "__main__":
    raise SystemExit(main())
