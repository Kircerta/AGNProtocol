#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
from uuid import uuid4

import jwt
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
REPORT_PATH = ROOT / "reports" / "agn_mvp_acceptance.json"
AUDIT_PATH = ROOT / "audit" / "events.jsonl"

REQUIRED_RUN_TASK_KEYS = [
    "ok",
    "task_id",
    "attempt",
    "decision",
    "commit_hash",
    "no_change_reason",
    "result_path",
    "verdict_path",
    "fail_reasons",
]
CHECK_IDS = [
    "a_run_agn_task_protocol_ok",
    "b_run_agn_task_contract_fields",
    "c_ingest_repo_missing_rejected_without_dispatch_pollution",
    "d_telegram_listener_repo_missing_rejected_without_dispatch_pollution",
    "e_external_publish_unapproved_denied_with_audit",
    "f_hallucination_lock_unlock_redispatch_chain",
]


@dataclass
class CheckResult:
    check_id: str
    passed: bool
    detail: str



def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()



def _truncate(text: str, max_chars: int = 600) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 16] + "...<truncated>"



def run_cmd(
    cmd: list[str],
    *,
    timeout_sec: float = 60.0,
    stdin_text: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        input=stdin_text,
        capture_output=True,
        timeout=timeout_sec,
        env=env,
    )



def _latest_task_file(prefix: str, directory: Path) -> Path | None:
    candidates = sorted(directory.glob(f"{prefix}*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return None
    return candidates[0]



def _scan_audit_for(task_id: str, action: str) -> bool:
    if not AUDIT_PATH.exists():
        return False
    for line in AUDIT_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if event.get("task_id") == task_id and event.get("action") == action:
            return True
    return False



def _check_run_agn_task_positive() -> tuple[CheckResult, CheckResult]:
    task_id = f"mvp-run-task-{uuid4().hex[:8]}"
    payload = {
        "task_id": task_id,
        "task_kind": "protocol",
        "source": "openclaw",
        "request_text": "mvp protocol positive",
    }
    proc = run_cmd(
        [sys.executable, "scripts/run_agn_task.py", "--from-stdin"],
        timeout_sec=60.0,
        stdin_text=json.dumps(payload, ensure_ascii=True),
    )
    if proc.returncode != 0:
        return (
            CheckResult("a_run_agn_task_protocol_ok", False, f"rc={proc.returncode} stderr={_truncate(proc.stderr)}"),
            CheckResult("b_run_agn_task_contract_fields", False, "run_agn_task failed"),
        )

    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        return (
            CheckResult("a_run_agn_task_protocol_ok", False, f"expected single JSON line, got {len(lines)}"),
            CheckResult("b_run_agn_task_contract_fields", False, "stdout is not single JSON object"),
        )

    try:
        decoded = json.loads(lines[0])
    except Exception as exc:
        return (
            CheckResult("a_run_agn_task_protocol_ok", False, f"json decode failed: {type(exc).__name__}"),
            CheckResult("b_run_agn_task_contract_fields", False, "stdout JSON decode failed"),
        )

    a_ok = bool(decoded.get("ok") is True)
    b_ok = all(key in decoded for key in REQUIRED_RUN_TASK_KEYS)
    return (
        CheckResult("a_run_agn_task_protocol_ok", a_ok, f"ok={decoded.get('ok')} task_id={decoded.get('task_id')}") ,
        CheckResult("b_run_agn_task_contract_fields", b_ok, f"keys_ok={b_ok}"),
    )



def _check_ingest_negative() -> CheckResult:
    task_id = f"mvp-neg-ingest-{uuid4().hex[:8]}"
    dispatch_file = ROOT / "dispatch" / f"{task_id}.json"
    if dispatch_file.exists():
        dispatch_file.unlink()

    proc = run_cmd(
        [
            sys.executable,
            "scripts/coordinator_ingest.py",
            "--task-id",
            task_id,
            "--task-kind",
            "repo",
            "--request-text",
            "mvp negative ingest",
        ],
        timeout_sec=20.0,
    )
    rejected = proc.returncode == 1
    polluted = dispatch_file.exists()
    ok = rejected and not polluted
    detail = f"rc={proc.returncode} dispatch_exists={polluted} stdout={_truncate(proc.stdout)}"
    return CheckResult("c_ingest_repo_missing_rejected_without_dispatch_pollution", ok, detail)



def _check_telegram_negative() -> CheckResult:
    task_id = f"mvp-neg-telegram-{uuid4().hex[:8]}"
    dispatch_file = ROOT / "dispatch" / f"{task_id}.json"
    if dispatch_file.exists():
        dispatch_file.unlink()

    payload = {
        "task_id": task_id,
        "task_kind": "repo",
        "request_text": "mvp negative telegram",
    }
    proc = run_cmd(
        [
            sys.executable,
            "scripts/telegram_listener.py",
            "--stdin",
            "--stdin-chat-id",
            "mvp-local",
            "--stdin-message-id",
            "1",
        ],
        timeout_sec=20.0,
        stdin_text=json.dumps(payload, ensure_ascii=True),
    )
    rejected = "task_kind=repo requires repo_path, work_branch" in proc.stdout
    polluted = dispatch_file.exists()
    ok = proc.returncode == 0 and rejected and not polluted
    detail = f"rc={proc.returncode} rejected={rejected} dispatch_exists={polluted}"
    return CheckResult("d_telegram_listener_repo_missing_rejected_without_dispatch_pollution", ok, detail)



def _check_external_publish_gate() -> CheckResult:
    task_id = f"mvp-side-effect-{uuid4().hex[:8]}"
    with tempfile.TemporaryDirectory(prefix="agn_mvp_repo_") as tmpdir:
        repo_path = Path(tmpdir)
        run_cmd(["git", "-C", str(repo_path), "init"], timeout_sec=20.0)

        ingest = run_cmd(
            [
                sys.executable,
                "scripts/coordinator_ingest.py",
                "--task-id",
                task_id,
                "--task-kind",
                "repo",
                "--request-text",
                "mvp side effect gate",
                "--repo-path",
                str(repo_path),
                "--work-branch",
                "codex/mvp-side-effect",
                "--side-effect-level",
                "external_publish",
                "--risk-level",
                "high",
            ],
            timeout_sec=30.0,
        )
        if ingest.returncode != 0:
            return CheckResult("e_external_publish_unapproved_denied_with_audit", False, f"ingest rc={ingest.returncode}")

        exec_proc = run_cmd(
            [
                sys.executable,
                "scripts/executor_worker.py",
                "--once",
                "--mode",
                "real",
                "--task-id",
                task_id,
            ],
            timeout_sec=60.0,
        )

    result_file = ROOT / "results" / f"{task_id}.1.json"
    if not result_file.exists():
        return CheckResult("e_external_publish_unapproved_denied_with_audit", False, "result file missing")

    result_payload = json.loads(result_file.read_text(encoding="utf-8"))
    denied = "external_publish_not_approved" in (result_payload.get("fail_reasons", []) or [])
    audited = _scan_audit_for(task_id, "side_effect_denied")
    ok = exec_proc.returncode == 0 and denied and audited
    detail = f"exec_rc={exec_proc.returncode} denied={denied} audited={audited}"
    return CheckResult("e_external_publish_unapproved_denied_with_audit", ok, detail)



def _check_lock_unlock_chain() -> CheckResult:
    task_id = f"mvp-lock-{uuid4().hex[:8]}"
    dispatch_file = ROOT / "dispatch" / f"{task_id}.json"

    for attempt in (1, 2, 3):
        ingest = run_cmd(
            [
                sys.executable,
                "scripts/coordinator_ingest.py",
                "--task-id",
                task_id,
                "--task-kind",
                "protocol",
                "--source",
                "agn_smoke",
                "--request-text",
                f"mvp lock attempt {attempt}",
                "--attempt",
                str(attempt),
            ],
            timeout_sec=20.0,
        )
        if ingest.returncode != 0:
            return CheckResult("f_hallucination_lock_unlock_redispatch_chain", False, f"ingest attempt {attempt} failed")

        if run_cmd(
            [sys.executable, "scripts/executor_worker.py", "--once", "--mode", "real", "--task-id", task_id],
            timeout_sec=30.0,
        ).returncode != 0:
            return CheckResult("f_hallucination_lock_unlock_redispatch_chain", False, f"executor attempt {attempt} failed")

        if run_cmd(
            [sys.executable, "scripts/reviewer_worker.py", "--once", "--mode", "real", "--task-id", task_id],
            timeout_sec=30.0,
            extra_env={"AGN_FAKE_REVIEWER_MODE": "always_reject"},
        ).returncode != 0:
            return CheckResult("f_hallucination_lock_unlock_redispatch_chain", False, f"reviewer attempt {attempt} failed")

    ssot_file = ROOT / "ssot" / f"{task_id}.json"
    if not ssot_file.exists():
        return CheckResult("f_hallucination_lock_unlock_redispatch_chain", False, "ssot task missing after retries")

    task_payload = json.loads(ssot_file.read_text(encoding="utf-8"))
    lock_halted = task_payload.get("lock_state") == "halted"

    if dispatch_file.exists():
        dispatch_file.unlink()
    run_cmd([sys.executable, "scripts/coordinator_loop.py", "--once"], timeout_sec=20.0)
    halted_blocks_dispatch = not dispatch_file.exists()

    jwt_secret = "mvp-unlock-secret-at-least-32-bytes"
    from agn_api.config import AppConfig
    from agn_api.main import create_app

    app = create_app(
        AppConfig(
            ssot_dir=ROOT / "ssot",
            audit_log_path=ROOT / "audit" / "events.jsonl",
            jwt_secret=jwt_secret,
            jwt_algorithm="HS256",
        )
    )
    token = jwt.encode(
        {
            "sub": "mvp-admin",
            "exp": datetime.now(tz=timezone.utc) + timedelta(minutes=10),
        },
        jwt_secret,
        algorithm="HS256",
    )
    with TestClient(app) as client:
        unlock_resp = client.post(
            f"/api/tasks/{task_id}/unlock",
            headers={"Authorization": f"Bearer {token}"},
        )
    unlock_ok = unlock_resp.status_code == 200 and unlock_resp.json().get("lock_state") == "active"

    run_cmd([sys.executable, "scripts/coordinator_loop.py", "--once"], timeout_sec=20.0)
    redispatch_ok = dispatch_file.exists()

    ok = lock_halted and halted_blocks_dispatch and unlock_ok and redispatch_ok
    detail = (
        f"lock_halted={lock_halted} halted_blocks_dispatch={halted_blocks_dispatch} "
        f"unlock_ok={unlock_ok} redispatch_ok={redispatch_ok}"
    )
    return CheckResult("f_hallucination_lock_unlock_redispatch_chain", ok, detail)



def main() -> int:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)

    checks: list[CheckResult] = []
    check_a, check_b = _check_run_agn_task_positive()
    checks.extend([check_a, check_b])
    checks.append(_check_ingest_negative())
    checks.append(_check_telegram_negative())
    checks.append(_check_external_publish_gate())
    checks.append(_check_lock_unlock_chain())

    passed = sum(1 for item in checks if item.passed)
    failed = len(checks) - passed
    mvp_ready = failed == 0 and sorted(item.check_id for item in checks) == sorted(CHECK_IDS)

    report = {
        "generated_at_utc": utc_now_iso(),
        "mvp_ready": mvp_ready,
        "summary": {
            "total": len(checks),
            "passed": passed,
            "failed": failed,
        },
        "checks": [
            {
                "id": item.check_id,
                "pass": item.passed,
                "detail": item.detail,
            }
            for item in checks
        ],
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({"mvp_ready": mvp_ready, "report": str(REPORT_PATH)}, ensure_ascii=True))
    return 0 if mvp_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
