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
    "tests/test_admin_command_protocol.py",
    "tests/test_package_commands.py",
    "tests/test_control_daemon.py",
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
    package_probe = _run(
        [
            sys.executable,
            "-c",
            (
                "from agn.governance import commands as acp; "
                "import json; "
                "print(json.dumps({'package_path': acp.PACKAGE_PATH, 'legacy_script_shim': acp.LEGACY_SCRIPT_SHIM, 'has_submit': callable(acp.submit_admin_command), 'has_ack': callable(acp.ack_admin_command)}, ensure_ascii=True))"
            ),
        ],
        extra_env={"PYTHONPATH": f"{ROOT / 'src'}:{ROOT / 'scripts'}"},
    )
    package_payload = json.loads(package_probe.stdout)
    script_probe = _run(
        [
            sys.executable,
            "-c",
            (
                "import admin_command_protocol as acp; "
                "import json; "
                "print(json.dumps({'has_submit': callable(acp.submit_admin_command), 'has_validate': callable(acp.validate_admin_command)}, ensure_ascii=True))"
            ),
        ],
        extra_env={"PYTHONPATH": f"{ROOT / 'src'}:{ROOT / 'scripts'}"},
    )
    script_payload = json.loads(script_probe.stdout)

    shim_text = (ROOT / "scripts" / "admin_command_protocol.py").read_text(encoding="utf-8")
    package_text = (ROOT / "src" / "agn" / "governance" / "commands.py").read_text(encoding="utf-8")
    system_text = (ROOT / "src" / "agn" / "governance" / "system.py").read_text(encoding="utf-8")
    daemon_text = (ROOT / "src" / "agn" / "governance" / "control_daemon.py").read_text(encoding="utf-8")
    posture_text = (ROOT / "scripts" / "agn_control_plane_operator_posture.py").read_text(encoding="utf-8")

    return {
        "package_import": {
            "returncode": int(package_probe.returncode),
            "package_path": str(package_payload.get("package_path", "")),
            "legacy_script_shim": str(package_payload.get("legacy_script_shim", "")),
            "pass": int(package_probe.returncode) == 0
            and str(package_payload.get("package_path", "")) == "agn.governance.commands"
            and str(package_payload.get("legacy_script_shim", "")) == "scripts/admin_command_protocol.py"
            and bool(package_payload.get("has_submit", False))
            and bool(package_payload.get("has_ack", False)),
        },
        "script_shim_runtime": {
            "returncode": int(script_probe.returncode),
            "has_submit": bool(script_payload.get("has_submit", False)),
            "has_validate": bool(script_payload.get("has_validate", False)),
            "pass": int(script_probe.returncode) == 0 and bool(script_payload.get("has_submit", False)) and bool(script_payload.get("has_validate", False)),
        },
        "script_shim_static": {
            "shim_import_present": "from agn.governance.commands import *" in shim_text,
            "pass": "from agn.governance.commands import *" in shim_text,
        },
        "package_is_real_impl": {
            "submit_present": "def submit_admin_command(" in package_text,
            "validate_present": "def validate_admin_command(" in package_text,
            "pass": "def submit_admin_command(" in package_text and "def validate_admin_command(" in package_text,
        },
        "system_import_updated": {
            "package_import_present": "from agn.governance.commands import submit_admin_command" in system_text,
            "pass": "from agn.governance.commands import submit_admin_command" in system_text,
        },
        "control_daemon_import_updated": {
            "package_import_present": "from agn.governance.commands import (" in daemon_text,
            "pass": "from agn.governance.commands import (" in daemon_text,
        },
        "operator_posture_import_updated": {
            "package_import_present": "from agn.governance.commands import COMMANDS, normalize_admin_command" in posture_text,
            "pass": "from agn.governance.commands import COMMANDS, normalize_admin_command" in posture_text,
        },
    }


def build_report() -> dict[str, Any]:
    tests = _run_tests()
    checks = _migration_checks()
    overall_pass = tests["pass"] and all(bool(item["pass"]) for item in checks.values())
    return {
        "schema_version": "agn.validation.phase3_commands_migration.v1",
        "generated_at": utc_now_iso(),
        "objective": "Verify that AGN's formal admin command protocol now lives in agn.governance.commands while the legacy script remains a compatibility shim and active governance surfaces import the package implementation directly.",
        "binary_verdict": {
            "targeted_tests_green": bool(tests["pass"]),
            "package_import_works": bool(checks["package_import"]["pass"]),
            "script_shim_runtime_works": bool(checks["script_shim_runtime"]["pass"]),
            "script_shim_present": bool(checks["script_shim_static"]["pass"]),
            "package_is_real_impl": bool(checks["package_is_real_impl"]["pass"]),
            "system_import_updated": bool(checks["system_import_updated"]["pass"]),
            "control_daemon_import_updated": bool(checks["control_daemon_import_updated"]["pass"]),
            "operator_posture_import_updated": bool(checks["operator_posture_import_updated"]["pass"]),
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
    report_path = REPORT_DIR / f"{timestamp}-phase3-commands-migration-acceptance.json"
    latest = REPORT_DIR / "phase3-commands-migration-acceptance.latest.json"
    atomic_write_json(report_path, payload)
    atomic_write_json(latest, payload)
    print(json.dumps({**payload, "report_path": str(report_path)}, ensure_ascii=True, indent=2))
    return 0 if payload["binary_verdict"]["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
