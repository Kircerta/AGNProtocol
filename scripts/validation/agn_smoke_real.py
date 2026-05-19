#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]
DISPATCH_DIR = ROOT / "dispatch"
ACK_DIR = DISPATCH_DIR / "acks"
RESULTS_DIR = ROOT / "results"
VERDICTS_DIR = ROOT / "verdicts"
AUDIT_PATH = ROOT / "audit" / "events.jsonl"
REPORT_JSON_PATH = ROOT / "reports" / "agn_smoke_real_report.json"
REPORT_LOG_PATH = ROOT / "reports" / "agn_smoke_real.log"


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def run_cmd(
    args: list[str],
    *,
    cwd: Path,
    timeout_sec: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=timeout_sec,
    )


def stop_background_agents() -> None:
    # Best-effort stop to avoid long-running agn-up workers consuming the same dispatch.
    try:
        subprocess.run(
            ["bash", "scripts/agn_down.sh"],
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=15.0,
        )
    except Exception:
        pass
    for pattern in (
        "scripts/coordinator_loop.py",
        "scripts/executor_worker.py --interval-seconds",
        "scripts/reviewer_worker.py --interval-seconds",
        "uvicorn agn_api.main:app --host 127.0.0.1 --port 8000",
    ):
        try:
            subprocess.run(
                ["pkill", "-f", pattern],
                cwd=str(ROOT),
                text=True,
                capture_output=True,
                timeout=10.0,
            )
        except Exception:
            pass


def setup_minimal_repo() -> Path:
    repo_dir = Path(tempfile.mkdtemp(prefix="agn_smoke_real_repo_"))
    (repo_dir / "Sources").mkdir(parents=True, exist_ok=True)
    (repo_dir / "README.md").write_text("# AGN smoke real\n", encoding="utf-8")
    (repo_dir / "Sources" / "LaunchDB.swift").write_text(
        "\n".join(
            [
                "import Foundation",
                "",
                "func launchDatabaseURL(from input: String) -> URL? {",
                "    // Intentional anti-pattern for codex to fix",
                "    return URL(string: input)",
                "}",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    run_cmd(["git", "init"], cwd=repo_dir, timeout_sec=30.0)
    run_cmd(["git", "config", "user.email", "agn-smoke@example.com"], cwd=repo_dir, timeout_sec=30.0)
    run_cmd(["git", "config", "user.name", "AGN Smoke"], cwd=repo_dir, timeout_sec=30.0)
    run_cmd(["git", "add", "-A"], cwd=repo_dir, timeout_sec=30.0)
    run_cmd(["git", "commit", "-m", "chore: seed smoke repo"], cwd=repo_dir, timeout_sec=30.0)
    return repo_dir


def main() -> int:
    REPORT_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    DISPATCH_DIR.mkdir(parents=True, exist_ok=True)
    ACK_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    VERDICTS_DIR.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)

    stop_background_agents()

    task_id = f"agn-real-{uuid4().hex[:8]}"
    correlation_id = f"corr-{uuid4().hex[:12]}"
    work_branch = f"codex/agn-real-{uuid4().hex[:6]}"
    reviewer_provider = str(os.getenv("REVIEWER_PROVIDER", "gemini") or "gemini").strip().lower()
    if reviewer_provider not in {"claude", "gemini", "deepseek"}:
        reviewer_provider = "gemini"
    dispatch_file = DISPATCH_DIR / f"{task_id}.json"
    ack_file = ACK_DIR / f"{task_id}.1.json"
    result_file = RESULTS_DIR / f"{task_id}.1.json"
    verdict_file = VERDICTS_DIR / f"{task_id}.1.json"
    criteria = [
        "AC-1:result must contain commit_hash or no_change_reason and non-placeholder diff",
        "AC-2:commands_ran and work_log must contain real command evidence",
        "AC-3:reviewer issues must include criterion_ref and evidence",
    ]
    logs: list[str] = []
    fail_reasons: list[str] = []

    def log(message: str) -> None:
        line = f"[{utc_now_iso()}] {message}"
        logs.append(line)
        print(line)

    repo_path = setup_minimal_repo()
    log(f"created minimal repo: {repo_path}")

    commands = [
        (
            "coordinator_ingest",
            [
                sys.executable,
                "scripts/coordinator_ingest.py",
                "--task-id",
                task_id,
                "--source",
                "agn_smoke_real",
                "--task-kind",
                "repo",
                "--request-text",
                "Fix launch database error; prioritize URL(string:) to filesystem URL conversion",
                "--correlation-id",
                correlation_id,
                "--repo-path",
                str(repo_path),
                "--work-branch",
                work_branch,
                "--criterion",
                criteria[0],
                "--criterion",
                criteria[1],
                "--criterion",
                criteria[2],
                "--executor-provider",
                "codex",
                "--reviewer-provider",
                reviewer_provider,
            ],
            40.0,
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
            1200.0,
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
            400.0,
        ),
    ]

    command_outputs: dict[str, dict[str, Any]] = {}
    for name, cmd, timeout_sec in commands:
        log(f"run {name}: {' '.join(cmd)}")
        try:
            proc = run_cmd(cmd, cwd=ROOT, timeout_sec=timeout_sec)
            command_outputs[name] = {
                "return_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }
            log(f"{name} rc={proc.returncode}")
            if proc.returncode != 0:
                fail_reasons.append(f"{name} failed rc={proc.returncode}")
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            command_outputs[name] = {
                "return_code": 124,
                "stdout": stdout,
                "stderr": stderr,
            }
            fail_reasons.append(f"{name} timed out")
            log(f"{name} timeout")

    for label, path in (
        ("dispatch", dispatch_file),
        ("ack", ack_file),
        ("result", result_file),
        ("verdict", verdict_file),
    ):
        if not path.exists():
            fail_reasons.append(f"missing {label} file")

    metrics: dict[str, Any] = {
        "task_id": task_id,
        "repo_path": str(repo_path),
        "work_branch": work_branch,
        "dispatch_has_repo": False,
        "dispatch_has_branch": False,
        "non_placeholder_result": False,
        "has_commit_or_no_change_reason": False,
        "review_traceable": False,
        "reviewer_decision": "",
    }

    if dispatch_file.exists() and result_file.exists() and verdict_file.exists():
        dispatch_payload = load_json(dispatch_file)
        result_payload = load_json(result_file)
        verdict_payload = load_json(verdict_file)
        criteria_payload = dispatch_payload.get("acceptance_criteria", [])
        criteria_ids = {
            str(item.get("id"))
            for item in criteria_payload
            if isinstance(item, dict) and str(item.get("id", "")).strip()
        }

        metrics["dispatch_has_repo"] = bool(str(dispatch_payload.get("repo_path", "")).strip())
        metrics["dispatch_has_branch"] = bool(str(dispatch_payload.get("work_branch", "")).strip())
        if not metrics["dispatch_has_repo"]:
            fail_reasons.append("dispatch missing repo_path")
        if not metrics["dispatch_has_branch"]:
            fail_reasons.append("dispatch missing work_branch")

        diff_snapshot = str(result_payload.get("diff_snapshot", ""))
        work_log = result_payload.get("work_log", [])
        commands_ran = result_payload.get("commands_ran", [])
        contains_placeholder = "placeholder diff snapshot for phase d" in diff_snapshot.lower()
        legacy_worklog = any(str(item.get("op", "")).startswith("operation_") for item in work_log if isinstance(item, dict))
        metrics["non_placeholder_result"] = (
            not contains_placeholder and not legacy_worklog and bool(commands_ran)
        )
        if not metrics["non_placeholder_result"]:
            fail_reasons.append("result still looks like placeholder output")

        commit_hash = str(result_payload.get("commit_hash") or "").strip()
        no_change_reason = str(result_payload.get("no_change_reason") or "").strip()
        if no_change_reason and diff_snapshot != "no changes":
            fail_reasons.append("no_change_reason set but diff_snapshot is not 'no changes'")
        metrics["has_commit_or_no_change_reason"] = bool(commit_hash or no_change_reason)
        if not metrics["has_commit_or_no_change_reason"]:
            fail_reasons.append("result missing commit_hash and no_change_reason")

        traceable = True
        issues = verdict_payload.get("issues", [])
        metrics["reviewer_decision"] = str(verdict_payload.get("decision", "")).strip().lower()
        if metrics["reviewer_decision"] != "approve":
            fail_reasons.append(f"reviewer decision is not approve: {metrics['reviewer_decision']}")
        if not isinstance(issues, list):
            traceable = False
            fail_reasons.append("verdict issues is not list")
            issues = []
        work_log_len = len(work_log) if isinstance(work_log, list) else 0
        for idx, issue in enumerate(issues):
            if not isinstance(issue, dict):
                traceable = False
                fail_reasons.append(f"issue[{idx}] invalid type")
                continue
            criterion_ref = str(issue.get("criterion_ref", "")).strip()
            if criterion_ref not in criteria_ids:
                traceable = False
                fail_reasons.append(f"issue[{idx}] invalid criterion_ref")
            evidence = issue.get("evidence")
            if not isinstance(evidence, dict):
                # Backward compatibility for legacy verdict schema.
                evidence = issue.get("evidence_ref")
            if not isinstance(evidence, dict):
                traceable = False
                fail_reasons.append(f"issue[{idx}] missing evidence")
                continue
            if isinstance(evidence.get("work_log_index"), int):
                wl_index = int(evidence["work_log_index"])
                if wl_index < 0 or wl_index >= work_log_len:
                    traceable = False
                    fail_reasons.append(f"issue[{idx}] work_log_index out of range")
            elif evidence.get("artifact_path"):
                artifact_candidate = Path(str(evidence["artifact_path"]))
                if not artifact_candidate.is_absolute():
                    artifact_candidate = ROOT / artifact_candidate
                if not artifact_candidate.exists():
                    traceable = False
                    fail_reasons.append(f"issue[{idx}] artifact_path missing")
            else:
                traceable = False
                fail_reasons.append(f"issue[{idx}] evidence has no supported reference")
        metrics["review_traceable"] = traceable

    pass_flag = len(fail_reasons) == 0
    report = {
        "pass": pass_flag,
        "generated_at_utc": utc_now_iso(),
        "metrics": metrics,
        "fail_reasons": fail_reasons,
        "command_outputs": command_outputs,
        "artifacts": [],
    }

    artifact_paths = [
        path
        for path in [
            dispatch_file,
            ack_file,
            result_file,
            verdict_file,
            AUDIT_PATH,
            ROOT / "reports" / f"agn_executor_{task_id}.1_exec.log",
            ROOT / "reports" / f"agn_reviewer_{task_id}.1_exec.log",
        ]
        if path.exists()
    ]
    for path in artifact_paths:
        report["artifacts"].append(
            {
                "path": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
                "sha256": sha256_file(path),
            }
        )

    REPORT_JSON_PATH.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    REPORT_LOG_PATH.write_text("\n".join(logs) + "\n", encoding="utf-8")
    final_rc = 0 if pass_flag else 1
    verdict = str(metrics.get("reviewer_decision") or "unknown")
    print(f"TASK_ID={task_id} RC={final_rc} VERDICT={verdict}")
    return final_rc


if __name__ == "__main__":
    raise SystemExit(main())
