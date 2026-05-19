#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]
DISPATCH_DIR = ROOT / "dispatch"
ACK_DIR = DISPATCH_DIR / "acks"
RESULTS_DIR = ROOT / "results"
VERDICTS_DIR = ROOT / "verdicts"
AUDIT_PATH = ROOT / "audit" / "events.jsonl"
REPORT_JSON_PATH = ROOT / "reports" / "agn_smoke_report.json"
REPORT_LOG_PATH = ROOT / "reports" / "agn_smoke.log"


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_cmd(args: list[str], *, timeout_sec: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=timeout_sec,
    )


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    REPORT_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    DISPATCH_DIR.mkdir(parents=True, exist_ok=True)
    ACK_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    VERDICTS_DIR.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)

    task_id = f"agn-smoke-{uuid4().hex[:8]}"
    correlation_id = f"corr-{uuid4().hex[:12]}"
    criteria = [
        "AC-1:ack echo exact match",
        "AC-2:result includes work_log",
        "AC-3:verdict issue references criterion id",
    ]

    fail_reasons: list[str] = []
    logs: list[str] = []

    def log(message: str) -> None:
        line = f"[{utc_now_iso()}] {message}"
        logs.append(line)
        print(line)

    commands = [
        (
            "coordinator_ingest",
            [
                sys.executable,
                "scripts/coordinator_ingest.py",
                "--task-id",
                task_id,
                "--source",
                "agn_smoke",
                "--task-kind",
                "protocol",
                "--request-text",
                "smoke protocol validation",
                "--correlation-id",
                correlation_id,
                "--criterion",
                criteria[0],
                "--criterion",
                criteria[1],
                "--criterion",
                criteria[2],
            ],
        ),
        (
            "executor_worker",
            [
                sys.executable,
                "scripts/executor_worker.py",
                "--once",
                "--mode",
                "real",
                "--task-id",
                task_id,
            ],
        ),
        (
            "reviewer_worker",
            [
                sys.executable,
                "scripts/reviewer_worker.py",
                "--once",
                "--mode",
                "real",
                "--task-id",
                task_id,
            ],
        ),
    ]

    command_outputs: dict[str, dict[str, Any]] = {}
    for name, cmd in commands:
        log(f"run {name}: {' '.join(cmd)}")
        try:
            proc = run_cmd(cmd, timeout_sec=20.0)
        except subprocess.TimeoutExpired as exc:
            fail_reasons.append(f"{name} timed out")
            command_outputs[name] = {
                "return_code": 124,
                "stdout": exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
                "stderr": exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or ""),
            }
            continue

        command_outputs[name] = {
            "return_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
        log(f"{name} rc={proc.returncode}")
        if proc.returncode != 0:
            fail_reasons.append(f"{name} failed rc={proc.returncode}")

    dispatch_path = DISPATCH_DIR / f"{task_id}.json"
    ack_path = ACK_DIR / f"{task_id}.1.json"
    result_path = RESULTS_DIR / f"{task_id}.1.json"
    verdict_path = VERDICTS_DIR / f"{task_id}.1.json"

    for label, path in (
        ("dispatch", dispatch_path),
        ("ack", ack_path),
        ("result", result_path),
        ("verdict", verdict_path),
    ):
        if not path.exists():
            fail_reasons.append(f"missing {label} file: {path}")

    metrics: dict[str, Any] = {
        "task_id": task_id,
        "correlation_id": correlation_id,
        "ack_echo_exact_match": False,
        "criterion_ref_valid": False,
        "evidence_index_valid": False,
        "work_log_non_empty": False,
        "audit_has_required_events": False,
    }

    if dispatch_path.exists() and ack_path.exists() and result_path.exists() and verdict_path.exists():
        dispatch_payload = load_json(dispatch_path)
        ack_payload = load_json(ack_path)
        result_payload = load_json(result_path)
        verdict_payload = load_json(verdict_path)

        expected_criteria = dispatch_payload.get("acceptance_criteria", [])
        echoed_criteria = ack_payload.get("echoed_acceptance_criteria", [])
        metrics["ack_echo_exact_match"] = echoed_criteria == expected_criteria
        if not metrics["ack_echo_exact_match"]:
            fail_reasons.append("ack echoed_acceptance_criteria mismatch")

        issues = verdict_payload.get("issues", [])
        work_log = result_payload.get("work_log", [])
        metrics["work_log_non_empty"] = isinstance(work_log, list) and len(work_log) > 0
        if not metrics["work_log_non_empty"]:
            fail_reasons.append("result work_log is empty")
        criterion_ids = {
            str(item["id"])
            for item in expected_criteria
            if isinstance(item, dict) and "id" in item
        }

        criterion_ref_valid = True
        evidence_index_valid = True
        for issue in issues if isinstance(issues, list) else []:
            if not isinstance(issue, dict):
                criterion_ref_valid = False
                evidence_index_valid = False
                continue
            criterion_ref = str(issue.get("criterion_ref", ""))
            if criterion_ref not in criterion_ids:
                criterion_ref_valid = False
            evidence = issue.get("evidence", {})
            index = evidence.get("work_log_index") if isinstance(evidence, dict) else None
            if not isinstance(index, int) or index < 0 or index >= len(work_log):
                evidence_index_valid = False

        metrics["criterion_ref_valid"] = criterion_ref_valid
        metrics["evidence_index_valid"] = evidence_index_valid
        if not criterion_ref_valid:
            fail_reasons.append("verdict issues contain invalid criterion_ref")
        if not evidence_index_valid:
            fail_reasons.append("verdict issues contain invalid evidence index")

    if AUDIT_PATH.exists():
        required_actions = {
            "dispatch_created",
            "executor_ack_written",
            "executor_processed",
            "reviewer_processed",
        }
        seen_actions: set[str] = set()
        for line in AUDIT_PATH.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            if not isinstance(event, dict):
                continue
            if str(event.get("task_id", "")).strip() != task_id:
                continue
            action = str(event.get("action", "")).strip()
            if action in required_actions:
                seen_actions.add(action)
        metrics["audit_has_required_events"] = seen_actions == required_actions
        if not metrics["audit_has_required_events"]:
            missing = sorted(required_actions - seen_actions)
            fail_reasons.append(f"audit missing required actions: {','.join(missing)}")
    else:
        fail_reasons.append("missing audit file")

    pass_flag = len(fail_reasons) == 0
    report = {
        "pass": pass_flag,
        "generated_at_utc": utc_now_iso(),
        "metrics": metrics,
        "fail_reasons": fail_reasons,
        "command_outputs": command_outputs,
        "artifacts": [],
    }

    artifact_paths = [path for path in [dispatch_path, ack_path, result_path, verdict_path, AUDIT_PATH] if path.exists()]
    for path in artifact_paths:
        report["artifacts"].append(
            {"path": str(path.relative_to(ROOT)), "sha256": sha256_file(path)}
        )

    REPORT_JSON_PATH.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    REPORT_LOG_PATH.write_text("\n".join(logs) + "\n", encoding="utf-8")
    return 0 if pass_flag else 1


if __name__ == "__main__":
    raise SystemExit(main())
