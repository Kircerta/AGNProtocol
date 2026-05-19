#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import time

from agent_runner import (
    append_audit,
    list_dispatches,
    result_path,
    run_executor_codex,
    run_loop,
)
from validation.fake_executor import run as run_executor

# Per-task timeout for the executor worker (default 1200s = 20 min).
# The subprocess itself has a 900s timeout, but this is a safety net for the
# entire task processing including setup, verification, git operations, etc.
_EXECUTOR_TASK_TIMEOUT = max(60, int(os.getenv("AGN_EXECUTOR_TASK_TIMEOUT", "1200") or "1200"))


def _dispatch_task_kind(dispatch: dict[str, object]) -> str:
    raw = str(dispatch.get("task_kind", "")).strip().lower()
    if raw in {"repo", "protocol"}:
        return raw
    return "protocol"


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
        if result_file.exists():
            skipped += 1
            continue

        task_kind = _dispatch_task_kind(dispatch)
        use_real_executor = mode == "real" and task_kind == "repo"

        # P2-13: worker-level timeout enforcement.
        # Uses a monotonic deadline to avoid threading.Timer race conditions.
        task_timed_out = False
        deadline = time.monotonic() + _EXECUTOR_TASK_TIMEOUT
        try:
            if use_real_executor:
                code, _ = run_executor_codex(dispatch)
            else:
                code = run_executor(task_id=task_id)
        except Exception:
            code = 1
        if time.monotonic() > deadline:
            task_timed_out = True

        # P2-BUG-FIX: Only apply timeout code when task did NOT succeed.
        # If code == 0, the task completed successfully before the deadline
        # was checked — overwriting with 124 would discard a valid result.
        if task_timed_out and code != 0:
            code = 124  # convention for timeout
            append_audit(
                action="executor_task_timeout",
                task_id=task_id,
                route="/agn/executor",
                status=504,
                attempt=attempt,
                timeout_sec=_EXECUTOR_TASK_TIMEOUT,
            )

        if code == 0:
            processed += 1
            if not use_real_executor:
                append_audit(
                    action="executor_processed",
                    task_id=task_id,
                    route="/agn/executor",
                    status=200,
                    attempt=attempt,
                    task_kind=task_kind,
                    worker_mode=mode,
                )
        else:
            errors += 1
            if not use_real_executor:
                append_audit(
                    action="executor_failed",
                    task_id=task_id,
                    route="/agn/executor",
                    status=500,
                    attempt=attempt,
                    rc=code,
                    task_kind=task_kind,
                    worker_mode=mode,
                )

    return {"processed": processed, "skipped": skipped, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description="Executor worker processing dispatch files")
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-per-tick", type=int, default=20)
    parser.add_argument("--mode", choices=["real", "fake"], default="real")
    parser.add_argument("--task-id", help="Process only one task_id")
    args = parser.parse_args()

    os.environ.setdefault("AGN_ROLE", "executor")
    os.environ.setdefault("AGN_RUNTIME_CONTEXT", "agn_network")
    os.environ.setdefault("AGN_ENFORCE_ROLE_GUARD", "1")

    max_per_tick = max(1, args.max_per_tick)
    return run_loop(
        worker_name="executor",
        interval_seconds=max(0.1, args.interval_seconds),
        once=args.once,
        handler=lambda: process_once(max_per_tick=max_per_tick, mode=args.mode, task_filter=args.task_id),
    )


if __name__ == "__main__":
    raise SystemExit(main())
