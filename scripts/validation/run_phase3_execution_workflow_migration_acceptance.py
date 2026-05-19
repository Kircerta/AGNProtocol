#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from admin_control_common import atomic_write_json


REPORT_DIR = ROOT / "reports" / "validation"
TEST_COMMAND = [
    "uvx",
    "pytest",
    "tests/test_agn2_execution_workflow.py",
    "tests/test_package_execution_workflow.py",
    "tests/test_agn_memory_recall.py",
    "tests/test_agn_host_info.py",
    "-q",
]


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _run(cmd: list[str], *, timeout_sec: float = 180.0, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged_env = None
    if extra_env is not None:
        import os

        merged_env = os.environ.copy()
        merged_env.update(extra_env)
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=timeout_sec,
        check=False,
        env=merged_env,
    )


def _run_tests() -> dict[str, Any]:
    completed = _run(TEST_COMMAND)
    stdout = str(completed.stdout or "")
    passed_count = 0
    for line in stdout.splitlines():
        if " passed" in line:
            parts = line.strip().split()
            if parts and parts[0].isdigit():
                passed_count = int(parts[0])
                break
    return {
        "command": TEST_COMMAND,
        "returncode": int(completed.returncode),
        "passed_count": passed_count,
        "stdout_tail": stdout.strip().splitlines()[-5:],
        "stderr_tail": str(completed.stderr or "").strip().splitlines()[-5:],
        "pass": completed.returncode == 0,
    }


def _migration_checks() -> dict[str, Any]:
    script_preflight = _run(
        [
            sys.executable,
            "scripts/agn2_execution_workflow.py",
            "preflight",
            "--task-summary",
            "Phase 3 execution workflow migration check",
            "--no-write",
        ]
    )
    script_payload = json.loads(script_preflight.stdout)
    package_probe = _run(
        [
            sys.executable,
            "-c",
            (
                "from agn.governance import execution_workflow as wf; "
                "import json; "
                "print(json.dumps({'package_path': wf.PACKAGE_PATH, 'legacy_script_shim': wf.LEGACY_SCRIPT_SHIM, 'has_preflight': callable(wf.build_preflight_payload)}, ensure_ascii=True))"
            ),
        ],
        extra_env={"PYTHONPATH": f"{ROOT / 'src'}:{ROOT / 'scripts'}"},
    )
    package_payload = json.loads(package_probe.stdout)

    shim_text = (ROOT / "scripts" / "agn2_execution_workflow.py").read_text(encoding="utf-8")
    package_text = (ROOT / "src" / "agn" / "governance" / "execution_workflow.py").read_text(encoding="utf-8")
    task_start_text = (ROOT / "src" / "agn" / "governance" / "task_start_kernel.py").read_text(encoding="utf-8")
    brief_text = (ROOT / "src" / "agn" / "governance" / "operator_brief.py").read_text(encoding="utf-8")
    rhythm_text = (ROOT / "scripts" / "agn_capability_rhythm.py").read_text(encoding="utf-8")
    delegation_text = (ROOT / "scripts" / "agn_bounded_delegation.py").read_text(encoding="utf-8")

    return {
        "script_preflight": {
            "returncode": int(script_preflight.returncode),
            "package_path": str((script_payload.get("task_start_kernel", {}) or {}).get("package_path", "")),
            "pass": int(script_preflight.returncode) == 0 and bool(script_payload.get("ok", False)),
        },
        "package_import": {
            "returncode": int(package_probe.returncode),
            "package_path": str(package_payload.get("package_path", "")),
            "legacy_script_shim": str(package_payload.get("legacy_script_shim", "")),
            "pass": int(package_probe.returncode) == 0
            and str(package_payload.get("package_path", "")) == "agn.governance.execution_workflow"
            and str(package_payload.get("legacy_script_shim", "")) == "scripts/agn2_execution_workflow.py"
            and bool(package_payload.get("has_preflight", False)),
        },
        "script_shim_static": {
            "shim_import_present": "from agn.governance.execution_workflow import *" in shim_text,
            "pass": "from agn.governance.execution_workflow import *" in shim_text,
        },
        "package_is_real_impl": {
            "preflight_present": "def build_preflight_payload(" in package_text,
            "delegate_present": "def build_delegate_request(" in package_text,
            "pass": "def build_preflight_payload(" in package_text and "def build_delegate_request(" in package_text,
        },
        "task_start_import": {
            "package_import_present": "from agn.governance.execution_workflow import system_snapshot" in task_start_text,
            "pass": "from agn.governance.execution_workflow import system_snapshot" in task_start_text,
        },
        "operator_brief_import": {
            "package_import_present": "from agn.governance.execution_workflow import build_preflight_payload" in brief_text,
            "pass": "from agn.governance.execution_workflow import build_preflight_payload" in brief_text,
        },
        "capability_rhythm_import": {
            "package_import_present": "from agn.governance.execution_workflow import build_preflight_payload, system_snapshot" in rhythm_text,
            "pass": "from agn.governance.execution_workflow import build_preflight_payload, system_snapshot" in rhythm_text,
        },
        "bounded_delegation_import": {
            "package_import_present": "from agn.governance.execution_workflow import build_delegate_request" in delegation_text,
            "pass": "from agn.governance.execution_workflow import build_delegate_request" in delegation_text,
        },
    }


def build_report() -> dict[str, Any]:
    tests = _run_tests()
    checks = _migration_checks()
    overall_pass = tests["pass"] and all(bool(item["pass"]) for item in checks.values())
    return {
        "schema_version": "agn.validation.phase3_execution_workflow_migration.v1",
        "generated_at": utc_now_iso(),
        "objective": "Verify that execution workflow now lives in agn.governance.execution_workflow while the legacy script remains a shim and active task-start consumers import the package implementation directly.",
        "binary_verdict": {
            "targeted_tests_green": bool(tests["pass"]),
            "script_preflight_works": bool(checks["script_preflight"]["pass"]),
            "package_import_works": bool(checks["package_import"]["pass"]),
            "script_shim_present": bool(checks["script_shim_static"]["pass"]),
            "package_is_real_impl": bool(checks["package_is_real_impl"]["pass"]),
            "task_start_uses_package_import": bool(checks["task_start_import"]["pass"]),
            "operator_brief_uses_package_import": bool(checks["operator_brief_import"]["pass"]),
            "capability_rhythm_uses_package_import": bool(checks["capability_rhythm_import"]["pass"]),
            "bounded_delegation_uses_package_import": bool(checks["bounded_delegation_import"]["pass"]),
            "overall_pass": bool(overall_pass),
        },
        "counts": {
            "tests_passed": int(tests["passed_count"]),
            "checks_passed": int(sum(1 for item in checks.values() if item["pass"])),
        },
        "test_run": tests,
        "migration_checks": checks,
    }


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = build_report()
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = REPORT_DIR / f"{timestamp}-phase3-execution-workflow-migration-acceptance.json"
    latest = REPORT_DIR / "phase3-execution-workflow-migration-acceptance.latest.json"
    atomic_write_json(report_path, payload)
    atomic_write_json(latest, payload)
    print(json.dumps({**payload, "report_path": str(report_path)}, ensure_ascii=True, indent=2))
    return 0 if payload["binary_verdict"]["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
