#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import tempfile
from typing import Any

from pointer_protocol import write_json_artifact


ROOT = Path(__file__).resolve().parents[2]
DISPATCH_DIR = ROOT / "dispatch"
ACK_DIR = DISPATCH_DIR / "acks"
RESULTS_DIR = ROOT / "results"
VERDICTS_DIR = ROOT / "verdicts"


# Modes:
# - ac_based (default): evaluate ACs and approve/reject accordingly.
# - always_reject: preserve legacy behavior for lock-stress tests.
FAKE_REVIEWER_MODE = str(os.getenv("AGN_FAKE_REVIEWER_MODE", "ac_based")).strip().lower() or "ac_based"


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=True, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def load_dispatch(task_id: str) -> dict[str, Any]:
    path = DISPATCH_DIR / f"{task_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"dispatch not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_ack(task_id: str, attempt: int) -> dict[str, Any]:
    path = ACK_DIR / f"{task_id}.{attempt}.json"
    if not path.exists():
        raise FileNotFoundError(f"ack not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_result(task_id: str, attempt: int) -> dict[str, Any]:
    path = RESULTS_DIR / f"{task_id}.{attempt}.json"
    if not path.exists():
        raise FileNotFoundError(f"result not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _legacy_build_issues(criteria: list[dict[str, Any]], work_log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not work_log:
        return issues

    for i, criterion in enumerate(criteria):
        log_index = i % len(work_log)
        work_entry = work_log[log_index]
        issues.append(
            {
                "criterion_ref": str(criterion.get("id", f"AC-{i+1}")),
                "id": f"issue-{i+1}",
                "title": f"Criterion {criterion.get('id', f'AC-{i+1}')} requires follow-up",
                "detail": f"Observed incomplete evidence for: {criterion.get('text', '')}",
                "evidence": {
                    "work_log_index": log_index,
                    "work_log_op": work_entry.get("op"),
                },
            }
        )
    return issues


def _make_issue(
    *,
    issue_id: str,
    criterion_ref: str,
    title: str,
    detail: str,
    work_log: list[dict[str, Any]],
    preferred_index: int = 0,
    artifact_path: str = "",
) -> dict[str, Any]:
    if work_log:
        index = max(0, min(preferred_index, len(work_log) - 1))
        op = str(work_log[index].get("op", "")).strip()
        evidence: dict[str, Any] = {"work_log_index": index, "work_log_op": op}
    else:
        evidence = {"artifact_path": artifact_path}

    return {
        "criterion_ref": criterion_ref,
        "id": issue_id,
        "title": title,
        "detail": detail,
        "evidence": evidence,
    }


def _criterion_id(item: dict[str, Any], idx: int) -> str:
    return str(item.get("id") or f"AC-{idx}").strip()


def _criterion_text(item: dict[str, Any]) -> str:
    return str(item.get("text") or "").strip().lower()


def _is_ack_echo_criterion(criterion_id: str, text: str) -> bool:
    if criterion_id == "AC-1":
        return True
    return "ack" in text and "echo" in text


def _is_work_log_criterion(criterion_id: str, text: str) -> bool:
    if criterion_id == "AC-2":
        return True
    return "work log" in text


def _is_evidence_criterion(criterion_id: str, text: str) -> bool:
    if criterion_id == "AC-3":
        return True
    return "criterion" in text and "evidence" in text


def _work_log_valid(work_log: list[Any]) -> bool:
    if len(work_log) < 5:
        return False
    for item in work_log:
        if not isinstance(item, dict):
            return False
        if not str(item.get("ts", "")).strip():
            return False
        if not str(item.get("op", "")).strip():
            return False
        if not str(item.get("detail", "")).strip():
            return False
    return True


def _evaluate_ac_issues(
    *,
    task_id: str,
    attempt: int,
    criteria: list[dict[str, Any]],
    work_log: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    ack_ok = False
    try:
        ack_payload = load_ack(task_id, attempt)
        ack_ok = (
            str(ack_payload.get("task_id", "")).strip() == task_id
            and int(ack_payload.get("attempt", 0) or 0) == attempt
            and ack_payload.get("echoed_acceptance_criteria") == criteria
        )
    except Exception:
        ack_ok = False

    work_log_ok = _work_log_valid(work_log)

    for idx, criterion in enumerate(criteria, start=1):
        if not isinstance(criterion, dict):
            continue
        cid = _criterion_id(criterion, idx)
        ctext = _criterion_text(criterion)
        title = f"Criterion {cid} failed"

        if _is_ack_echo_criterion(cid, ctext):
            if not ack_ok:
                issues.append(
                    _make_issue(
                        issue_id=f"issue-{len(issues)+1}",
                        criterion_ref=cid,
                        title=title,
                        detail="ack missing or echoed_acceptance_criteria does not exactly match dispatch",
                        work_log=work_log,
                        artifact_path=f"dispatch/acks/{task_id}.{attempt}.json",
                    )
                )
            continue

        if _is_work_log_criterion(cid, ctext):
            if not work_log_ok:
                issues.append(
                    _make_issue(
                        issue_id=f"issue-{len(issues)+1}",
                        criterion_ref=cid,
                        title=title,
                        detail="work_log missing required structure or has fewer than 5 entries",
                        work_log=work_log,
                        artifact_path=f"results/{task_id}.{attempt}.json",
                    )
                )
            continue

        if _is_evidence_criterion(cid, ctext):
            # AC-3 is satisfied by construction when generated issues include criterion_ref + evidence.
            continue

    return issues


def run(task_id: str) -> int:
    try:
        dispatch = load_dispatch(task_id)
    except Exception as exc:
        print(f"reviewer error: {exc}")
        return 1

    attempt = int(dispatch.get("attempt", 0))
    if attempt <= 0:
        print("reviewer error: invalid attempt")
        return 1

    try:
        result = load_result(task_id, attempt)
    except Exception as exc:
        print(f"reviewer error: {exc}")
        return 1

    criteria = dispatch.get("acceptance_criteria", [])
    work_log = result.get("work_log", [])
    if not isinstance(criteria, list) or not isinstance(work_log, list):
        print("reviewer error: invalid criteria/work_log")
        return 1

    if FAKE_REVIEWER_MODE == "always_reject":
        issues = _legacy_build_issues(criteria, work_log)
    else:
        issues = _evaluate_ac_issues(
            task_id=task_id,
            attempt=attempt,
            criteria=criteria,
            work_log=work_log,
        )

    decision = "reject" if issues else "approve"
    review_context_ref = ""
    try:
        ref = write_json_artifact(
            task_id=task_id,
            attempt=attempt,
            artifact_id="review_context",
            payload={"dispatch": dispatch, "result": result},
            filename="review_context.json",
            source="fake_reviewer",
        )
        review_context_ref = ref.ref
    except Exception:
        review_context_ref = ""

    verdict_payload = {
        "task_id": task_id,
        "correlation_id": str(dispatch.get("correlation_id", result.get("correlation_id", ""))),
        "attempt": attempt,
        "decision": decision,
        "issues": issues,
        "lazy_loading_protocol": "pointer_v1",
        "artifact_refs": result.get("artifact_refs", []),
        "review_context_ref": review_context_ref,
        "verdict_at": utc_now_iso(),
    }
    verdict_path = VERDICTS_DIR / f"{task_id}.{attempt}.json"
    atomic_write_json(verdict_path, verdict_payload)
    print(f"verdict written: {verdict_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Fake reviewer for phase D protocol")
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()
    return run(task_id=args.task_id)


if __name__ == "__main__":
    raise SystemExit(main())
