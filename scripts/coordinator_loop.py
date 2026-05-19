#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import os
import time
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
    from agent_runner import (
        PATHS,
        append_audit,
        atomic_write_json,
        dispatch_path,
        ensure_dirs,
        latest_attempt_for,
        load_json,
        run_loop,
        utc_now_iso,
        verdict_path,
    )
except ImportError:  # pragma: no cover - package import fallback
    from scripts.agent_runner import (
        PATHS,
        append_audit,
        atomic_write_json,
        dispatch_path,
        ensure_dirs,
        latest_attempt_for,
        load_json,
        run_loop,
        utc_now_iso,
        verdict_path,
    )
try:
    from coordinator_ingest import DEFAULT_ACCEPTANCE_CRITERIA
except ImportError:  # pragma: no cover - package import fallback
    from scripts.coordinator_ingest import DEFAULT_ACCEPTANCE_CRITERIA
try:
    from pointer_protocol import ref_to_artifact_entry, write_text_artifact
except ImportError:  # pragma: no cover - package import fallback
    from scripts.pointer_protocol import ref_to_artifact_entry, write_text_artifact
try:
    from network_runtime import acknowledge_coordinator_refresh, publish_runtime_surface
except ImportError:  # pragma: no cover - package import fallback
    from scripts.network_runtime import acknowledge_coordinator_refresh, publish_runtime_surface

# ── AGN2.0 governance bridge ──
try:
    from agn.governance.bridge import evaluate_agn1_dispatch as _agn2_evaluate_dispatch, emit_agn1_audit as _agn2_emit_audit
except ImportError:  # pragma: no cover - package import fallback
    from scripts.agn2_governance_bridge import evaluate_agn1_dispatch as _agn2_evaluate_dispatch, emit_agn1_audit as _agn2_emit_audit

DONE_DIR = PATHS.dispatch_dir / "done"
DEAD_LETTER_DIR = PATHS.root / "runtime" / "dead_letter"
# Stale dispatch timeout: dispatches older than this (seconds) with no result
# are considered stuck and eligible for re-dispatch (default 30 min).
_STALE_DISPATCH_TIMEOUT = max(300, int(os.getenv("AGN_STALE_DISPATCH_TIMEOUT", "1800") or "1800"))
_DISPATCH_REQUEST_INLINE_LIMIT = max(512, int(os.getenv("AGN_DISPATCH_REQUEST_INLINE_LIMIT", "4096") or "4096"))
_DISPATCH_REQUEST_SUMMARY_LIMIT = max(120, int(os.getenv("AGN_DISPATCH_REQUEST_SUMMARY_LIMIT", "480") or "480"))


def _summarize_request_text(text: str) -> str:
    raw = str(text or "").strip()
    if len(raw) <= _DISPATCH_REQUEST_SUMMARY_LIMIT:
        return raw
    return raw[: _DISPATCH_REQUEST_SUMMARY_LIMIT - 24] + "...<truncated-summary>..."


def _recover_stale_dispatches(store: SSOTStore) -> int:
    """Detect dispatch files with no result that exceed the staleness timeout.

    For each stale dispatch, mark the SSOT task as rejected so the normal
    retry path re-dispatches it on the next tick.
    """
    recovered = 0
    now = time.time()
    for dp in sorted(PATHS.dispatch_dir.glob("*.json")):
        try:
            payload = load_json(dp)
            task_id = str(payload.get("task_id", "")).strip()
            attempt = int(payload.get("attempt", 0) or 0)
            if not task_id or attempt <= 0:
                continue
            from agent_runner import result_path as _rp
            if _rp(task_id, attempt).exists():
                continue  # result exists — not stale
            age = now - dp.stat().st_mtime
            if age < _STALE_DISPATCH_TIMEOUT:
                continue
            # Stale: mark task as rejected to trigger retry.
            # P2-BUG-FIX: use locked_update instead of get_task+save_task
            # to prevent TOCTOU race with concurrent reviewer writes.
            with store.locked_update(task_id) as task:
                if task is None:
                    continue
                if task.get("decision") == "approved" or task.get("lock_state") == "halted":
                    continue
                task["decision"] = "rejected"
                task["reviewed_at"] = utc_now_iso()
                append_audit(
                    action="stale_dispatch_recovered",
                    task_id=task_id,
                    route="/agn/coordinator",
                    status=200,
                    attempt=attempt,
                    stale_age_sec=int(age),
                )
                recovered += 1
        except Exception:
            continue
    return recovered


def _needs_dispatch(task: dict[str, Any]) -> tuple[bool, int]:
    """Return (should_dispatch, next_attempt).

    Returns True for:
    - New tasks that have no dispatch file yet (attempt=1)
    - Rejected tasks that have not been re-dispatched yet (attempt=N+1)

    Returns False for:
    - Approved tasks (final state)
    - Halted/locked tasks
    - Tasks already dispatched and awaiting result/verdict
    """
    if not isinstance(task, dict):
        return False, 0
    if not task.get("agn_managed"):
        return False, 0
    task_id = str(task.get("id", "")).strip()
    if not task_id:
        return False, 0
    if str(task.get("lock_state", "")).strip().lower() == "halted":
        return False, 0

    decision = task.get("decision")
    current_status = derive_status(task)

    # Approved tasks are final — never re-dispatch.
    if decision == "approved" or current_status == "approved":
        return False, 0

    dp = dispatch_path(task_id)

    # Task was rejected — eligible for retry.
    if decision == "rejected" or current_status == "rejected":
        # Find the latest attempt that has a verdict, compute next attempt.
        last_verdict_attempt = latest_attempt_for(task_id, PATHS.verdicts_dir)
        if last_verdict_attempt <= 0:
            last_verdict_attempt = latest_attempt_for(task_id, PATHS.results_dir)
        next_attempt = max(2, last_verdict_attempt + 1)
        # Archive old dispatch so a new one can be created.
        if dp.exists():
            DONE_DIR.mkdir(parents=True, exist_ok=True)
            archived = DONE_DIR / dp.name
            dp.rename(archived)
        return True, next_attempt

    # New task — no dispatch file yet.
    if not dp.exists():
        return True, 1

    return False, 0


def _criteria_for_task(task: dict[str, Any]) -> list[dict[str, str]]:
    raw = task.get("acceptance_criteria")
    if not isinstance(raw, list) or not raw:
        return [dict(item) for item in DEFAULT_ACCEPTANCE_CRITERIA]
    criteria: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        criterion_id = str(item.get("id", "")).strip()
        text = str(item.get("text", "")).strip()
        if criterion_id and text:
            criteria.append({"id": criterion_id, "text": text})
    if criteria:
        return criteria
    return [dict(item) for item in DEFAULT_ACCEPTANCE_CRITERIA]


def _archive_completed_dispatches(store: SSOTStore) -> int:
    """P2-10: Move dispatch files for approved tasks to done/ to reduce scan overhead."""
    archived = 0
    DONE_DIR.mkdir(parents=True, exist_ok=True)
    for dp in sorted(PATHS.dispatch_dir.glob("*.json")):
        try:
            payload = load_json(dp)
            task_id = str(payload.get("task_id", "")).strip()
            if not task_id:
                continue
            task = store.get_task(task_id)
            if task and task.get("decision") == "approved":
                dp.rename(DONE_DIR / dp.name)
                archived += 1
        except Exception:
            continue
    return archived


def process_once(max_per_tick: int) -> dict[str, int]:
    ensure_dirs()
    store = SSOTStore(PATHS.ssot_dir)

    # P2-10: archive completed dispatches to reduce scan overhead.
    _archive_completed_dispatches(store)

    # P1-6: detect and recover stale dispatches (no result after timeout).
    _recover_stale_dispatches(store)

    tasks = store.list_tasks()

    processed = 0
    skipped = 0
    errors = 0

    for task in tasks:
        if str(task.get("lock_state", "")).strip().lower() == "halted":
            task_id = str(task.get("id", "")).strip() or None
            if task_id and not str(task.get("lock_notified_at", "")).strip():
                dl_path = DEAD_LETTER_DIR / f"{task_id.replace('/', '_')}.json"
                with store.locked_update(task_id) as locked_task:
                    if locked_task is not None:
                        locked_task["lock_notified_at"] = utc_now_iso()
                        # P3-16: park halted tasks in dead_letter/ so they don't block scans.
                        DEAD_LETTER_DIR.mkdir(parents=True, exist_ok=True)
                        if not dl_path.exists():
                            atomic_write_json(dl_path, dict(locked_task))
                append_audit(
                    action="hallucination_lock_triggered",
                    task_id=task_id,
                    route="/agn/coordinator",
                    status=423,
                    correlation_id=str(task.get("correlation_id", "")).strip(),
                    lock_state="halted",
                    lock_reason=str(task.get("lock_reason", "")).strip(),
                    qa_retry_count=int(task.get("qa_retry_count", 0) or 0),
                    dead_letter=str(dl_path),
                )
            skipped += 1
            continue
        if processed >= max_per_tick:
            skipped += 1
            continue
        should_dispatch, next_attempt = _needs_dispatch(task)
        if not should_dispatch:
            skipped += 1
            continue

        try:
            task_id = str(task["id"])
            correlation_id = str(task.get("correlation_id") or "").strip()
            if not correlation_id:
                # P3-17: generate and persist so SSOT and dispatch share the same ID.
                correlation_id = f"corr-{uuid4().hex[:12]}"
                with store.locked_update(task_id) as _ltask:
                    if _ltask is not None:
                        # Re-check under lock in case another coordinator set it.
                        existing = str(_ltask.get("correlation_id") or "").strip()
                        if existing:
                            correlation_id = existing
                        else:
                            _ltask["correlation_id"] = correlation_id
                task["correlation_id"] = correlation_id
            criteria = _criteria_for_task(task)
            repo_path = str(task.get("repo_path", "")).strip()
            work_branch = str(task.get("work_branch", "")).strip()
            task_kind = str(task.get("task_kind", "")).strip().lower()
            if task_kind not in {"protocol", "repo"}:
                task_kind = "repo" if repo_path and work_branch else "protocol"

            is_retry = next_attempt > 1
            risk_level = str(task.get("risk_level", "low")).strip().lower() or "low"
            side_effect_level = str(task.get("side_effect_level", "read_only")).strip().lower() or "read_only"

            # ── AGN2.0 policy gate evaluation ──
            # Evaluate this dispatch against AGN2.0's governance rules.
            # If a policy gate rule fires, the dispatch is blocked until
            # the Admin approves it via the Control Plane.
            gate_result = _agn2_evaluate_dispatch(
                task_id=task_id,
                risk_level=risk_level,
                side_effect_level=side_effect_level,
                request_summary=_summarize_request_text(str(task.get("request_text", "")).strip()),
                correlation_id=correlation_id,
            )
            # P2-BUG-FIX: default to False (fail-safe) if 'allowed' key
            # is missing from gate_result, matching the governance bridge fix.
            if not gate_result.get("allowed", False):
                append_audit(
                    action="dispatch_gated_by_policy",
                    task_id=task_id,
                    route="/dispatch",
                    status=202,
                    correlation_id=correlation_id,
                    gate_id=str(gate_result.get("gate_id", "")),
                    rule_id=str(gate_result.get("rule_id", "")),
                    risk_level=risk_level,
                )
                skipped += 1
                continue
            # ── end AGN2.0 gate ──

            # For retries: reset SSOT decision so task re-enters the pipeline.
            # P2-BUG-FIX: use locked_update to prevent concurrent reviewer
            # writes from persisting "rejected" over the reset.
            if is_retry:
                with store.locked_update(task_id) as retry_task:
                    if retry_task is not None:
                        retry_task["decision"] = None
                        retry_task["reviewed_at"] = ""
                        retry_task["review_requested"] = True
                        retry_task["status"] = derive_status(retry_task)

            payload = {
                "task_id": task_id,
                "correlation_id": correlation_id,
                "attempt": next_attempt,
                "acceptance_criteria": criteria,
                "task_kind": task_kind,
                "lazy_loading_protocol": "pointer_v1",
                "source": str(task.get("source", "coordinator")).strip(),
                "repo_path": repo_path,
                "work_branch": work_branch,
                "executor_provider": str(task.get("executor_provider", "")).strip(),
                "reviewer_provider": str(task.get("reviewer_provider", "")).strip(),
                "chat_id": str(task.get("chat_id", "")).strip(),
                "message_id": str(task.get("message_id", "")).strip(),
                "risk_level": risk_level,
                "side_effect_level": side_effect_level,
            }
            request_text = str(task.get("request_text", "")).strip()
            payload["request_summary"] = _summarize_request_text(request_text)
            try:
                instruction_ref = write_text_artifact(
                    task_id=task_id,
                    attempt=next_attempt,
                    artifact_id="instructions",
                    content=request_text,
                    media_type="text/markdown",
                    filename="instructions.md",
                    source="coordinator_loop",
                )
                payload["artifact_refs"] = [ref_to_artifact_entry(instruction_ref)]
                payload["request_text_ref"] = instruction_ref.ref
            except Exception:
                payload["artifact_refs"] = []
            if request_text and len(request_text) <= _DISPATCH_REQUEST_INLINE_LIMIT:
                payload["request_text"] = request_text
            elif request_text:
                payload["request_text_mode"] = "ref_only"
            # P1-5 fix: use exclusive lock to prevent TOCTOU race when
            # multiple coordinator instances create the same dispatch.
            dp = dispatch_path(task_id)
            lock_dir = PATHS.dispatch_dir / ".locks"
            lock_dir.mkdir(parents=True, exist_ok=True)
            lock_file = lock_dir / f"{task_id}.lock"
            lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Re-check after acquiring lock.
                if dp.exists():
                    skipped += 1
                    continue
                atomic_write_json(dp, payload)
            except OSError:
                # Another process holds the lock — skip this task.
                skipped += 1
                continue
            finally:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(lock_fd)
                # P2-BUG-FIX: clean up lock file to prevent accumulation.
                try:
                    lock_file.unlink(missing_ok=True)
                except OSError:
                    pass
            append_audit(
                action="dispatch_created",
                task_id=task_id,
                route="/dispatch",
                status=200,
                correlation_id=correlation_id,
                attempt=next_attempt,
                criteria_count=len(criteria),
                task_kind=task_kind,
                is_retry=is_retry,
                request_summary_len=len(str(payload.get("request_summary", ""))),
                request_inline_len=len(str(payload.get("request_text", ""))),
                request_text_mode=str(payload.get("request_text_mode", "inline")),
            )
            # ── AGN2.0 audit: emit dispatch to admin trail ──
            _agn2_emit_audit(
                "dispatch_created",
                task_id=task_id,
                attempt=next_attempt,
                risk_level=risk_level,
                correlation_id=correlation_id,
            )
            processed += 1
        except Exception as exc:
            errors += 1
            append_audit(
                action="dispatch_failed",
                task_id=str(task.get("id")),
                route="/dispatch",
                status=500,
                error=type(exc).__name__,
                error_detail=str(exc)[:300],
            )

    return {"processed": processed, "skipped": skipped, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description="Coordinator loop that creates dispatch files from SSOT tasks")
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-per-tick", type=int, default=10)
    args = parser.parse_args()

    os.environ.setdefault("AGN_ROLE", "coordinator")
    os.environ.setdefault("AGN_RUNTIME_CONTEXT", "agn_network")
    os.environ.setdefault("AGN_ENFORCE_ROLE_GUARD", "1")
    publish_runtime_surface(reason="coordinator_loop_start")
    acknowledge_coordinator_refresh(actor="coordinator_loop", refresh_mode="startup")

    max_per_tick = max(1, args.max_per_tick)
    return run_loop(
        worker_name="coordinator",
        interval_seconds=max(0.1, args.interval_seconds),
        once=args.once,
        handler=lambda: process_once(max_per_tick=max_per_tick),
    )


if __name__ == "__main__":
    raise SystemExit(main())
