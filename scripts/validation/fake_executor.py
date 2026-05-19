#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import tempfile
from typing import Any

from agent_runner import append_audit
from pointer_protocol import ref_to_artifact_entry, write_text_artifact

ROOT = Path(__file__).resolve().parents[2]
DISPATCH_DIR = ROOT / "dispatch"
ACK_DIR = DISPATCH_DIR / "acks"
RESULTS_DIR = ROOT / "results"



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



def validate_dispatch(dispatch: dict[str, Any]) -> None:
    required = {"task_id", "correlation_id", "attempt", "acceptance_criteria"}
    missing = [k for k in required if k not in dispatch]
    if missing:
        raise ValueError(f"dispatch missing keys: {missing}")

    criteria = dispatch.get("acceptance_criteria")
    if not isinstance(criteria, list) or len(criteria) == 0:
        raise ValueError("acceptance_criteria must be non-empty list")

    for idx, criterion in enumerate(criteria):
        if not isinstance(criterion, dict):
            raise ValueError(f"criterion[{idx}] must be object")
        if "id" not in criterion or "text" not in criterion:
            raise ValueError(f"criterion[{idx}] missing id/text")



def build_work_log(task_id: str, attempt: int, criteria: list[dict[str, Any]]) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for i in range(5):
        criterion = criteria[i % len(criteria)]
        operations.append(
            {
                "ts": utc_now_iso(),
                "op": f"operation_{i+1}",
                "task_id": task_id,
                "attempt": attempt,
                "criterion_id": criterion["id"],
                "detail": f"processed {criterion['id']} at step {i+1}",
            }
        )
    return operations



def run(task_id: str) -> int:
    try:
        dispatch = load_dispatch(task_id)
        validate_dispatch(dispatch)
    except Exception as exc:
        print(f"executor error: {exc}")
        return 1

    attempt = int(dispatch["attempt"])
    correlation_id = str(dispatch["correlation_id"])
    criteria = dispatch["acceptance_criteria"]

    simulate_no_ack = os.getenv("SIMULATE_NO_ACK", "0") == "1"
    if not simulate_no_ack:
        ack_payload = {
            "task_id": task_id,
            "correlation_id": correlation_id,
            "attempt": attempt,
            "echoed_acceptance_criteria": [
                {"id": str(item["id"]), "text": str(item["text"])} for item in criteria
            ],
            "ack_at": utc_now_iso(),
        }
        ack_path = ACK_DIR / f"{task_id}.{attempt}.json"
        atomic_write_json(ack_path, ack_payload)
        append_audit(
            action="executor_ack_written",
            task_id=task_id,
            route="/dispatch/acks",
            status=200,
            attempt=attempt,
            correlation_id=correlation_id,
        )
        print(f"ack written: {ack_path}")
    else:
        print("ack skipped due to SIMULATE_NO_ACK=1")

    work_log = build_work_log(task_id, attempt, criteria)
    artifact_refs: list[dict[str, Any]] = []
    try:
        diff_ref = write_text_artifact(
            task_id=task_id,
            attempt=attempt,
            artifact_id="diff_snapshot",
            content="placeholder diff snapshot for phase d",
            media_type="text/x-diff",
            filename="diff_snapshot.patch",
            source="fake_executor",
        )
        artifact_refs.append(ref_to_artifact_entry(diff_ref))
    except Exception:
        pass

    try:
        work_log_ref = write_text_artifact(
            task_id=task_id,
            attempt=attempt,
            artifact_id="execution_log",
            content=json.dumps(work_log, ensure_ascii=True, indent=2),
            media_type="application/json",
            filename="execution_log.txt",
            source="fake_executor",
        )
        artifact_refs.append(ref_to_artifact_entry(work_log_ref))
    except Exception:
        pass

    result_payload = {
        "task_id": task_id,
        "correlation_id": correlation_id,
        "attempt": attempt,
        "work_log": work_log,
        "diff_snapshot": "placeholder diff snapshot for phase d",
        "lazy_loading_protocol": "pointer_v1",
        "artifact_refs": artifact_refs,
        "result_at": utc_now_iso(),
    }
    result_path = RESULTS_DIR / f"{task_id}.{attempt}.json"
    atomic_write_json(result_path, result_payload)
    print(f"result written: {result_path}")
    return 0



def main() -> int:
    parser = argparse.ArgumentParser(description="Fake executor for phase D protocol")
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()
    return run(task_id=args.task_id)


if __name__ == "__main__":
    raise SystemExit(main())
