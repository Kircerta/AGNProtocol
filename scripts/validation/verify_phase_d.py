#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from typing import Any
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DISPATCH_DIR = ROOT / "dispatch"
ACK_DIR = DISPATCH_DIR / "acks"
RESULTS_DIR = ROOT / "results"
VERDICTS_DIR = ROOT / "verdicts"
AUDIT_PATH = ROOT / "audit" / "events.jsonl"
REPORT_JSON_PATH = ROOT / "reports" / "phase_D_acceptance.json"
REPORT_MD_PATH = ROOT / "reports" / "phase_D_acceptance.md"
VERIFY_LOG_PATH = ROOT / "reports" / "phase_D_verify.log"
EXECUTOR_1_LOG = ROOT / "reports" / "phase_D_fake_executor_attempt1.log"
REVIEWER_1_LOG = ROOT / "reports" / "phase_D_fake_reviewer_attempt1.log"
EXECUTOR_NO_ACK_LOG = ROOT / "reports" / "phase_D_fake_executor_no_ack.log"
EXECUTOR_2_LOG = ROOT / "reports" / "phase_D_fake_executor_attempt2.log"
REVIEWER_2_LOG = ROOT / "reports" / "phase_D_fake_reviewer_attempt2.log"
FIDELITY_LOG = ROOT / "reports" / "phase_D_fidelity_audit.log"

TASK_ID = "phase-d-task-001"
TIMEOUT_TASK_ID = "phase-d-timeout-task"
TIMEOUT_MS = 600



def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()



def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()



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



def ensure_dirs() -> None:
    DISPATCH_DIR.mkdir(parents=True, exist_ok=True)
    ACK_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    VERDICTS_DIR.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)



def clear_runtime() -> None:
    for pattern in (
        DISPATCH_DIR.glob("*.json"),
        ACK_DIR.glob("*.json"),
        RESULTS_DIR.glob("*.json"),
        VERDICTS_DIR.glob("*.json"),
    ):
        for path in pattern:
            path.unlink(missing_ok=True)
    AUDIT_PATH.write_text("", encoding="utf-8")



def append_audit(**event: object) -> None:
    payload = {"timestamp": utc_now_iso()}
    payload.update(event)
    with AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=True))
        f.write("\n")



def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))



def run_cmd(
    args: list[str],
    *,
    extra_env: dict[str, str] | None = None,
    timeout_sec: float = 120.0,
    log_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    timed_out = False
    try:
        proc = subprocess.run(
            args,
            cwd=str(ROOT),
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout_text = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr_text = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        timeout_note = f"TIMEOUT_EXPIRED after {timeout_sec}s"
        stderr_text = (stderr_text + "\n" if stderr_text else "") + timeout_note
        proc = subprocess.CompletedProcess(args=args, returncode=124, stdout=stdout_text, stderr=stderr_text)

    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command_line = " ".join(args)
        lines = [
            f"timestamp={utc_now_iso()}",
            f"command={command_line}",
            f"return_code={proc.returncode}",
            f"timed_out={timed_out}",
            "--- STDOUT ---",
            proc.stdout or "",
            "--- STDERR ---",
            proc.stderr or "",
        ]
        log_path.write_text("\n".join(lines), encoding="utf-8")

    return proc



def render_markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    lines = [
        "# Phase D Acceptance Report",
        "",
        f"- pass: `{report['pass']}`",
        f"- issue_count: `{metrics['issue_count']}`",
        f"- timeout_recorded: `{metrics['timeout_recorded']}`",
        f"- fidelity_ok: `{metrics['fidelity_ok']}`",
        f"- ack_echo_exact_match: `{metrics['ack_echo_exact_match']}`",
        f"- criterion_ref_valid: `{metrics['criterion_ref_valid']}`",
        f"- evidence_index_valid: `{metrics['evidence_index_valid']}`",
        "",
        "## Artifacts",
    ]

    for item in report["artifacts"]:
        lines.append(f"- `{item['path']}` sha256=`{item['sha256']}`")

    lines.extend(
        [
            "",
            "## How To Run",
            "- `python3 -m pip install -r requirements.txt`",
            "- `make verify-phase-d`",
        ]
    )

    if report["fail_reasons"]:
        lines.append("")
        lines.append("## Fail Reasons")
        for reason in report["fail_reasons"]:
            lines.append(f"- {reason}")

    return "\n".join(lines) + "\n"



def main() -> int:
    ensure_dirs()
    clear_runtime()
    for path in (
        VERIFY_LOG_PATH,
        EXECUTOR_1_LOG,
        REVIEWER_1_LOG,
        EXECUTOR_NO_ACK_LOG,
        EXECUTOR_2_LOG,
        REVIEWER_2_LOG,
        FIDELITY_LOG,
    ):
        if path.exists():
            path.unlink()

    logs: list[str] = []
    fail_reasons: list[str] = []

    def log(msg: str) -> None:
        line = f"[{utc_now_iso()}] {msg}"
        logs.append(line)
        print(line)

    metrics: dict[str, Any] = {
        "generated_at_utc": utc_now_iso(),
        "issue_count": 0,
        "timeout_recorded": False,
        "fidelity_ok": False,
        "ack_echo_exact_match": False,
        "criterion_ref_valid": False,
        "evidence_index_valid": False,
        "audit_event_count": 0,
    }

    acceptance_criteria = [
        {"id": "AC-1", "text": "executor produces deterministic ack"},
        {"id": "AC-2", "text": "reviewer issues must map to criteria"},
        {"id": "AC-3", "text": "evidence references valid work_log index"},
    ]

    # Step 1: create dispatch with >=3 criteria
    dispatch_v1 = {
        "task_id": TASK_ID,
        "correlation_id": f"corr-{uuid4().hex[:8]}",
        "attempt": 1,
        "acceptance_criteria": acceptance_criteria,
    }
    dispatch_path = DISPATCH_DIR / f"{TASK_ID}.json"
    atomic_write_json(dispatch_path, dispatch_v1)
    append_audit(
        route="/dispatch",
        status=200,
        task_id=TASK_ID,
        action="dispatch_created",
        correlation_id=dispatch_v1["correlation_id"],
        attempt=1,
    )

    # Step 2: fake executor
    exec_run = run_cmd(
        [sys.executable, "scripts/validation/fake_executor.py", "--task-id", TASK_ID],
        log_path=EXECUTOR_1_LOG,
        timeout_sec=120.0,
    )
    log(f"fake_executor rc={exec_run.returncode}")
    if exec_run.stdout:
        for ln in exec_run.stdout.strip().splitlines():
            log(f"executor: {ln}")
    if exec_run.returncode != 0:
        fail_reasons.append("fake_executor failed")
    if exec_run.returncode == 124:
        fail_reasons.append("fake_executor timed out")

    # Step 3: fake reviewer
    reviewer_run = run_cmd(
        [sys.executable, "scripts/validation/fake_reviewer.py", "--task-id", TASK_ID],
        log_path=REVIEWER_1_LOG,
        timeout_sec=120.0,
    )
    log(f"fake_reviewer rc={reviewer_run.returncode}")
    if reviewer_run.stdout:
        for ln in reviewer_run.stdout.strip().splitlines():
            log(f"reviewer: {ln}")
    if reviewer_run.returncode != 0:
        fail_reasons.append("fake_reviewer failed")
    if reviewer_run.returncode == 124:
        fail_reasons.append("fake_reviewer timed out")

    ack_path = ACK_DIR / f"{TASK_ID}.1.json"
    result_path = RESULTS_DIR / f"{TASK_ID}.1.json"
    verdict_path = VERDICTS_DIR / f"{TASK_ID}.1.json"

    if not ack_path.exists():
        fail_reasons.append("missing ack for attempt 1")
    if not result_path.exists():
        fail_reasons.append("missing result for attempt 1")
    if not verdict_path.exists():
        fail_reasons.append("missing verdict for attempt 1")

    if ack_path.exists() and result_path.exists() and verdict_path.exists():
        ack = load_json(ack_path)
        result = load_json(result_path)
        verdict = load_json(verdict_path)

        # Step 4 validations
        echoed = ack.get("echoed_acceptance_criteria")
        metrics["ack_echo_exact_match"] = echoed == dispatch_v1["acceptance_criteria"]
        if not metrics["ack_echo_exact_match"]:
            fail_reasons.append("ack echoed_acceptance_criteria mismatch")

        criteria_ids = {item["id"] for item in dispatch_v1["acceptance_criteria"]}
        issues = verdict.get("issues", [])
        metrics["issue_count"] = len(issues) if isinstance(issues, list) else 0
        if not isinstance(issues, list):
            fail_reasons.append("verdict issues must be list")
            issues = []

        criterion_ref_valid = True
        evidence_index_valid = True
        work_log = result.get("work_log", [])

        for idx, issue in enumerate(issues):
            if not isinstance(issue, dict):
                criterion_ref_valid = False
                evidence_index_valid = False
                fail_reasons.append(f"issue[{idx}] must be object")
                continue

            criterion_ref = issue.get("criterion_ref")
            if criterion_ref not in criteria_ids:
                criterion_ref_valid = False
                fail_reasons.append(f"issue[{idx}] has invalid criterion_ref")

            evidence = issue.get("evidence")
            if not isinstance(evidence, dict):
                evidence_index_valid = False
                fail_reasons.append(f"issue[{idx}] evidence missing")
                continue

            work_idx = evidence.get("work_log_index")
            if not isinstance(work_idx, int) or not (0 <= work_idx < len(work_log)):
                evidence_index_valid = False
                fail_reasons.append(f"issue[{idx}] evidence work_log_index invalid")

        metrics["criterion_ref_valid"] = criterion_ref_valid
        metrics["evidence_index_valid"] = evidence_index_valid

    # Step 5: no-ack mode + timeout audit
    timeout_dispatch = {
        "task_id": TIMEOUT_TASK_ID,
        "correlation_id": f"corr-{uuid4().hex[:8]}",
        "attempt": 1,
        "acceptance_criteria": acceptance_criteria,
    }
    timeout_dispatch_path = DISPATCH_DIR / f"{TIMEOUT_TASK_ID}.json"
    atomic_write_json(timeout_dispatch_path, timeout_dispatch)
    append_audit(
        route="/dispatch",
        status=200,
        task_id=TIMEOUT_TASK_ID,
        action="dispatch_created",
        correlation_id=timeout_dispatch["correlation_id"],
        attempt=1,
    )

    no_ack_run = run_cmd(
        [sys.executable, "scripts/validation/fake_executor.py", "--task-id", TIMEOUT_TASK_ID],
        extra_env={"SIMULATE_NO_ACK": "1"},
        log_path=EXECUTOR_NO_ACK_LOG,
        timeout_sec=120.0,
    )
    log(f"fake_executor(no-ack) rc={no_ack_run.returncode}")
    if no_ack_run.stdout:
        for ln in no_ack_run.stdout.strip().splitlines():
            log(f"executor(no-ack): {ln}")
    if no_ack_run.returncode != 0:
        fail_reasons.append("fake_executor in no-ack mode failed")
    if no_ack_run.returncode == 124:
        fail_reasons.append("fake_executor in no-ack mode timed out")

    timeout_ack_path = ACK_DIR / f"{TIMEOUT_TASK_ID}.1.json"
    waited_ms = 0
    poll_interval_ms = 50
    while waited_ms < TIMEOUT_MS and not timeout_ack_path.exists():
        time.sleep(poll_interval_ms / 1000.0)
        waited_ms += poll_interval_ms

    if timeout_ack_path.exists():
        fail_reasons.append("no-ack simulation unexpectedly wrote ack")
    else:
        append_audit(
            route="/dispatch/acks",
            status=408,
            task_id=TIMEOUT_TASK_ID,
            action="ack_timeout",
            correlation_id=timeout_dispatch["correlation_id"],
            attempt=1,
            timeout_ms=TIMEOUT_MS,
        )

    # Step 6: redispatch + fidelity audit
    dispatch_v2 = {
        "task_id": TASK_ID,
        "correlation_id": f"corr-{uuid4().hex[:8]}",
        "attempt": 2,
        "acceptance_criteria": acceptance_criteria,
    }
    atomic_write_json(dispatch_path, dispatch_v2)
    append_audit(
        route="/dispatch",
        status=200,
        task_id=TASK_ID,
        action="redispatch_created",
        correlation_id=dispatch_v2["correlation_id"],
        attempt=2,
    )

    exec_run_v2 = run_cmd(
        [sys.executable, "scripts/validation/fake_executor.py", "--task-id", TASK_ID],
        log_path=EXECUTOR_2_LOG,
        timeout_sec=120.0,
    )
    reviewer_run_v2 = run_cmd(
        [sys.executable, "scripts/validation/fake_reviewer.py", "--task-id", TASK_ID],
        log_path=REVIEWER_2_LOG,
        timeout_sec=120.0,
    )
    log(f"fake_executor(attempt2) rc={exec_run_v2.returncode}")
    log(f"fake_reviewer(attempt2) rc={reviewer_run_v2.returncode}")
    if exec_run_v2.returncode != 0 or reviewer_run_v2.returncode != 0:
        fail_reasons.append("attempt2 executor/reviewer failed")
    if exec_run_v2.returncode == 124:
        fail_reasons.append("fake_executor(attempt2) timed out")
    if reviewer_run_v2.returncode == 124:
        fail_reasons.append("fake_reviewer(attempt2) timed out")

    fidelity_run = run_cmd(
        [
            sys.executable,
            "scripts/validation/fidelity_audit.py",
            "--task-id",
            TASK_ID,
            "--base-attempt",
            "1",
            "--candidate-attempt",
            "2",
        ],
        log_path=FIDELITY_LOG,
        timeout_sec=120.0,
    )
    log(f"fidelity_audit rc={fidelity_run.returncode}")
    if fidelity_run.stdout:
        for ln in fidelity_run.stdout.strip().splitlines():
            log(f"fidelity: {ln}")

    metrics["fidelity_ok"] = fidelity_run.returncode == 0
    if not metrics["fidelity_ok"]:
        fail_reasons.append("fidelity_audit failed")
    if fidelity_run.returncode == 124:
        fail_reasons.append("fidelity_audit timed out")

    # Collect timeout metric from audit.
    events = [json.loads(line) for line in AUDIT_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    metrics["audit_event_count"] = len(events)
    metrics["timeout_recorded"] = any(
        e.get("action") == "ack_timeout"
        and e.get("task_id") == TIMEOUT_TASK_ID
        and int(e.get("timeout_ms", -1)) == TIMEOUT_MS
        for e in events
    )
    if not metrics["timeout_recorded"]:
        fail_reasons.append("ack_timeout audit event missing")

    VERIFY_LOG_PATH.write_text("\n".join(logs) + "\n", encoding="utf-8")

    artifacts: list[dict[str, str]] = []
    artifact_candidates = [
        AUDIT_PATH,
        VERIFY_LOG_PATH,
        EXECUTOR_1_LOG,
        REVIEWER_1_LOG,
        EXECUTOR_NO_ACK_LOG,
        EXECUTOR_2_LOG,
        REVIEWER_2_LOG,
        FIDELITY_LOG,
        DISPATCH_DIR / f"{TASK_ID}.json",
        ACK_DIR / f"{TASK_ID}.1.json",
        RESULTS_DIR / f"{TASK_ID}.1.json",
        VERDICTS_DIR / f"{TASK_ID}.1.json",
        VERDICTS_DIR / f"{TASK_ID}.2.json",
    ]

    for artifact in artifact_candidates:
        if artifact.exists():
            artifacts.append(
                {
                    "path": str(artifact.relative_to(ROOT)),
                    "sha256": sha256_file(artifact),
                }
            )
        else:
            fail_reasons.append(f"missing artifact: {artifact}")

    passed = len(fail_reasons) == 0
    report = {
        "pass": passed,
        "metrics": metrics,
        "artifacts": artifacts,
        "how_to_run": [
            "python3 -m pip install -r requirements.txt",
            "make verify-phase-d",
        ],
        "fail_reasons": fail_reasons,
    }

    REPORT_JSON_PATH.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    md_text = render_markdown(report)
    REPORT_MD_PATH.write_text(md_text, encoding="utf-8")

    report["artifacts"].append(
        {
            "path": str(REPORT_MD_PATH.relative_to(ROOT)),
            "sha256": sha256_file(REPORT_MD_PATH),
        }
    )
    REPORT_JSON_PATH.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
