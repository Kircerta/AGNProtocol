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
    "tests/test_control_plane_read_model.py",
    "tests/test_package_read_models.py",
    "tests/test_agn2_system.py",
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
    refresh = _run([sys.executable, "scripts/control_plane_read_model.py", "refresh"])
    refresh_payload = json.loads(refresh.stdout)
    package_probe = _run(
        [
            sys.executable,
            "-c",
            (
                "from agn.governance import read_models as rm; "
                "import json; "
                "print(json.dumps({'package_path': rm.PACKAGE_PATH, 'legacy_script_shim': rm.LEGACY_SCRIPT_SHIM, 'has_refresh': callable(rm.refresh_read_models)}, ensure_ascii=True))"
            ),
        ],
        extra_env={"PYTHONPATH": f"{ROOT / 'src'}:{ROOT / 'scripts'}"},
    )
    package_payload = json.loads(package_probe.stdout)

    shim_text = (ROOT / "scripts" / "control_plane_read_model.py").read_text(encoding="utf-8")
    package_text = (ROOT / "src" / "agn" / "governance" / "read_models.py").read_text(encoding="utf-8")
    agn2_text = (ROOT / "src" / "agn" / "governance" / "system.py").read_text(encoding="utf-8")
    daemon_text = (ROOT / "src" / "agn" / "governance" / "control_daemon.py").read_text(encoding="utf-8")
    dispatcher_text = (ROOT / "src" / "agn" / "dispatch" / "dispatcher.py").read_text(encoding="utf-8")

    return {
        "script_refresh": {
            "returncode": int(refresh.returncode),
            "generated_count": len(refresh_payload.get("generated", [])) if isinstance(refresh_payload.get("generated", []), list) else 0,
            "pass": int(refresh.returncode) == 0 and bool(refresh_payload.get("ok", False)),
        },
        "package_import": {
            "returncode": int(package_probe.returncode),
            "package_path": str(package_payload.get("package_path", "")),
            "legacy_script_shim": str(package_payload.get("legacy_script_shim", "")),
            "pass": int(package_probe.returncode) == 0
            and str(package_payload.get("package_path", "")) == "agn.governance.read_models"
            and str(package_payload.get("legacy_script_shim", "")) == "scripts/control_plane_read_model.py"
            and bool(package_payload.get("has_refresh", False)),
        },
        "script_shim_static": {
            "shim_import_present": "from agn.governance.read_models import *" in shim_text,
            "pass": "from agn.governance.read_models import *" in shim_text,
        },
        "package_is_real_impl": {
            "build_overview_present": "def build_overview_model()" in package_text,
            "refresh_present": "def refresh_read_models()" in package_text,
            "pass": "def build_overview_model()" in package_text and "def refresh_read_models()" in package_text,
        },
        "agn2_system_import": {
            "package_import_present": "from agn.governance.read_models import refresh_read_models" in agn2_text,
            "pass": "from agn.governance.read_models import refresh_read_models" in agn2_text,
        },
        "control_daemon_import": {
            "package_import_present": "from agn.governance.read_models import refresh_read_models" in daemon_text,
            "pass": "from agn.governance.read_models import refresh_read_models" in daemon_text,
        },
        "dispatcher_package_import": {
            "package_import_present": "from agn.governance.read_models import refresh_read_models" in dispatcher_text,
            "pass": "from agn.governance.read_models import refresh_read_models" in dispatcher_text,
        },
    }


def build_report() -> dict[str, Any]:
    tests = _run_tests()
    checks = _migration_checks()
    overall_pass = tests["pass"] and all(bool(item["pass"]) for item in checks.values())
    return {
        "schema_version": "agn.validation.phase3_read_models_migration.v1",
        "generated_at": utc_now_iso(),
        "objective": "Verify that control-plane read model aggregation now lives in agn.governance.read_models while the legacy script remains a shim and active system surfaces import the package implementation directly.",
        "binary_verdict": {
            "targeted_tests_green": bool(tests["pass"]),
            "script_refresh_works": bool(checks["script_refresh"]["pass"]),
            "package_import_works": bool(checks["package_import"]["pass"]),
            "script_shim_present": bool(checks["script_shim_static"]["pass"]),
            "package_is_real_impl": bool(checks["package_is_real_impl"]["pass"]),
            "agn2_system_uses_package_import": bool(checks["agn2_system_import"]["pass"]),
            "control_daemon_uses_package_import": bool(checks["control_daemon_import"]["pass"]),
            "dispatcher_runtime_uses_package_import": bool(checks["dispatcher_package_import"]["pass"]),
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
    report_path = REPORT_DIR / f"{timestamp}-phase3-read-models-migration-acceptance.json"
    latest = REPORT_DIR / "phase3-read-models-migration-acceptance.latest.json"
    atomic_write_json(report_path, payload)
    atomic_write_json(latest, payload)
    print(json.dumps({**payload, "report_path": str(report_path)}, ensure_ascii=True, indent=2))
    return 0 if payload["binary_verdict"]["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
