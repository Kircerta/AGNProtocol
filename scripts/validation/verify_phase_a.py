#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

import jwt

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agn_api.ssot_store import SSOTStore


HOST = "127.0.0.1"
PORT = 8801
BASE_URL = f"http://{HOST}:{PORT}"
JWT_ALGO = "HS256"
DEFAULT_JWT_SECRET = "phase-a-secret-32-bytes-minimum-value"

SSOT_DIR = ROOT / "ssot"
AUDIT_PATH = ROOT / "audit" / "events.jsonl"
REPORT_JSON_PATH = ROOT / "reports" / "phase_A_acceptance.json"
REPORT_MD_PATH = ROOT / "reports" / "phase_A_acceptance.md"

TASK_APPROVE = "phase-a-approve"
TASK_REJECT = "phase-a-reject"



def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()



def request_json(method: str, path: str, token: str | None = None) -> tuple[int, dict]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(f"{BASE_URL}{path}", headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, json.loads(body) if body else {}



def wait_for_server(proc: subprocess.Popen[str], timeout_sec: float = 15.0) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if proc.poll() is not None:
            logs = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"Server exited early with code {proc.returncode}. Logs:\n{logs}")

        try:
            status_code, _ = request_json("GET", "/api/tasks")
            if status_code == 200:
                return
        except Exception:
            pass

        time.sleep(0.15)

    raise RuntimeError("Server did not become ready before timeout")



def build_jwt(secret: str, subject: str) -> str:
    payload = {
        "sub": subject,
        "exp": datetime.now(tz=timezone.utc) + timedelta(minutes=15),
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGO)



def write_seed_tasks() -> None:
    store = SSOTStore(SSOT_DIR)
    store.save_task(
        {
            "id": TASK_APPROVE,
            "title": "Phase A approval task",
            "review_requested": True,
        }
    )
    store.save_task(
        {
            "id": TASK_REJECT,
            "title": "Phase A rejection task",
            "review_requested": True,
        }
    )



def read_audit_events() -> list[dict]:
    if not AUDIT_PATH.exists():
        return []

    events: list[dict] = []
    for line in AUDIT_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        events.append(json.loads(line))
    return events



def render_markdown_report(report: dict) -> str:
    metrics = report["metrics"]
    artifacts = report["artifacts"]
    lines = [
        "# Phase A Acceptance Report",
        "",
        f"- pass: `{report['pass']}`",
        f"- generated_at_utc: `{metrics['generated_at_utc']}`",
        f"- detail_consistency_checks: `{metrics['detail_consistency_checks']}`",
        f"- detail_consistent: `{metrics['detail_consistent']}`",
        f"- approve_readback_status: `{metrics['approve_readback_status']}`",
        f"- reject_readback_status: `{metrics['reject_readback_status']}`",
        f"- audit_event_count: `{metrics['audit_event_count']}`",
        "",
        "## Artifacts",
    ]

    for item in artifacts:
        lines.append(f"- `{item['path']}` sha256=`{item['sha256']}`")

    lines.extend(
        [
            "",
            "## How To Run",
            "- `python3 -m pip install -r requirements.txt`",
            "- `pytest -q`",
            "- `make verify-phase-a`",
        ]
    )

    if report["fail_reasons"]:
        lines.append("")
        lines.append("## Fail Reasons")
        for reason in report["fail_reasons"]:
            lines.append(f"- {reason}")

    return "\n".join(lines) + "\n"



def ensure_paths() -> None:
    SSOT_DIR.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)



def main() -> int:
    ensure_paths()

    fail_reasons: list[str] = []
    metrics: dict[str, object] = {
        "generated_at_utc": datetime.now(tz=timezone.utc).isoformat(),
        "detail_consistency_checks": 10,
        "detail_consistent": False,
        "approve_readback_status": "unknown",
        "reject_readback_status": "unknown",
        "audit_event_count": 0,
    }

    # Clean audit log for this run.
    AUDIT_PATH.write_text("", encoding="utf-8")

    write_seed_tasks()

    jwt_secret = os.getenv("JWT_SECRET", DEFAULT_JWT_SECRET)
    env = os.environ.copy()
    env["SSOT_DIR"] = str(SSOT_DIR)
    env["AUDIT_LOG_PATH"] = str(AUDIT_PATH)
    env["JWT_SECRET"] = jwt_secret
    env["JWT_ALGORITHM"] = JWT_ALGO

    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "agn_api.main:app",
                "--host",
                HOST,
                "--port",
                str(PORT),
            ],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        wait_for_server(proc)

        # 1) Detail consistency x10.
        first_payload: dict | None = None
        consistent = True
        for _ in range(10):
            code, payload = request_json("GET", f"/api/tasks/{TASK_APPROVE}")
            if code != 200:
                consistent = False
                fail_reasons.append(f"detail check failed with status {code}")
                break
            if first_payload is None:
                first_payload = payload
            elif payload != first_payload:
                consistent = False
                fail_reasons.append("detail payload changed across 10 consecutive reads")
                break
        metrics["detail_consistent"] = consistent

        reviewer_token = build_jwt(jwt_secret, "phase-a-reviewer")

        # 2) Approve then immediate readback.
        t0 = time.perf_counter()
        approve_code, _ = request_json("POST", f"/api/tasks/{TASK_APPROVE}/approve", token=reviewer_token)
        approve_detail_code, approve_detail = request_json("GET", f"/api/tasks/{TASK_APPROVE}")
        t1 = time.perf_counter()
        metrics["approve_roundtrip_ms"] = round((t1 - t0) * 1000, 2)

        if approve_code != 200 or approve_detail_code != 200 or approve_detail.get("status") != "approved":
            metrics["approve_readback_status"] = "failed"
            fail_reasons.append(
                f"approve readback mismatch: post={approve_code}, get={approve_detail_code}, status={approve_detail.get('status')}"
            )
        else:
            metrics["approve_readback_status"] = "approved"

        # 3) Reject then immediate readback.
        t2 = time.perf_counter()
        reject_code, _ = request_json("POST", f"/api/tasks/{TASK_REJECT}/reject", token=reviewer_token)
        reject_detail_code, reject_detail = request_json("GET", f"/api/tasks/{TASK_REJECT}")
        t3 = time.perf_counter()
        metrics["reject_roundtrip_ms"] = round((t3 - t2) * 1000, 2)

        if reject_code != 200 or reject_detail_code != 200 or reject_detail.get("status") != "rejected":
            metrics["reject_readback_status"] = "failed"
            fail_reasons.append(
                f"reject readback mismatch: post={reject_code}, get={reject_detail_code}, status={reject_detail.get('status')}"
            )
        else:
            metrics["reject_readback_status"] = "rejected"

    except Exception as exc:  # pragma: no cover - verification flow guard
        fail_reasons.append(str(exc))
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=4)

    events = read_audit_events()
    metrics["audit_event_count"] = len(events)
    for event in events:
        if not {"route", "status", "task_id", "timestamp"}.issubset(event):
            fail_reasons.append("audit event missing required keys")
            break

    artifacts_candidates = [
        AUDIT_PATH,
        SSOT_DIR / f"{TASK_APPROVE}.json",
        SSOT_DIR / f"{TASK_REJECT}.json",
    ]

    artifacts = []
    for artifact in artifacts_candidates:
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
            "pytest -q",
            "make verify-phase-a",
        ],
        "fail_reasons": fail_reasons,
    }

    REPORT_JSON_PATH.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    md_text = render_markdown_report(report)
    REPORT_MD_PATH.write_text(md_text, encoding="utf-8")

    # Add report markdown hash to JSON for complete artifact list.
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
