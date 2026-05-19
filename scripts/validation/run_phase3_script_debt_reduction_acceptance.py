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
BASELINE_DIRECT_SCRIPT_IMPORTS = 6
BASELINE_SCRIPTS_BOOTSTRAP = 13
BASELINE_REPORT = "reports/validation/20260325T045910Z-phase3-self-audit.json"
PYTEST_TARGETS = [
    "tests/test_control_daemon.py",
    "tests/test_package_control_daemon.py",
    "tests/test_agn2_system.py",
    "tests/test_package_execution_gateway.py",
    "tests/test_event_sourcing.py",
    "tests/test_runtime_bus.py",
    "tests/test_dispatcher_runtime.py",
    "tests/test_agn_infrastructure_map.py",
    "tests/test_agn_evolution_pipeline.py",
    "tests/test_admin_command_protocol.py",
    "tests/test_agn_reconstruction_status.py",
    "tests/test_agn2_execution_workflow.py",
]


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _run(cmd: list[str], *, timeout_sec: float = 300.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=timeout_sec,
        check=False,
    )


def _safe_json(raw: str) -> tuple[dict[str, Any], str]:
    try:
        payload = json.loads(str(raw or "{}"))
    except json.JSONDecodeError as exc:
        return {}, f"json_decode_error:{exc}"
    return payload if isinstance(payload, dict) else {}, ""


def _run_targeted_pytest() -> dict[str, Any]:
    completed = _run(["uvx", "pytest", *PYTEST_TARGETS, "-q"], timeout_sec=600.0)
    lines = str(completed.stdout or "").strip().splitlines()
    summary_line = lines[-1] if lines else ""
    return {
        "returncode": int(completed.returncode),
        "pass": int(completed.returncode) == 0,
        "summary_line": summary_line,
        "stdout_tail": lines[-3:],
        "stderr_tail": str(completed.stderr or "").strip().splitlines()[-3:],
    }


def _run_self_audit() -> dict[str, Any]:
    completed = _run([sys.executable, "scripts/validation/run_phase3_self_audit.py"], timeout_sec=600.0)
    payload, parse_error = _safe_json(completed.stdout)
    inventory = payload.get("inventory", {}) if isinstance(payload.get("inventory"), dict) else {}
    verdict = payload.get("binary_verdict", {}) if isinstance(payload.get("binary_verdict"), dict) else {}
    direct_count = len(inventory.get("real_modules_with_direct_script_imports", [])) if isinstance(inventory.get("real_modules_with_direct_script_imports"), list) else -1
    bootstrap_count = len(inventory.get("real_modules_with_scripts_path_bootstrap", [])) if isinstance(inventory.get("real_modules_with_scripts_path_bootstrap"), list) else -1
    return {
        "returncode": int(completed.returncode),
        "parse_error": parse_error,
        "report_path": str(payload.get("report_path", "")).strip(),
        "phase3_matrix_green": bool(verdict.get("phase3_matrix_green")),
        "overall_pass": bool(verdict.get("overall_pass")),
        "direct_script_import_count": direct_count,
        "scripts_bootstrap_count": bootstrap_count,
        "direct_script_imports_eliminated": direct_count == 0,
        "bootstrap_reduced_from_baseline": bootstrap_count >= 0 and bootstrap_count < BASELINE_SCRIPTS_BOOTSTRAP,
        "stdout_tail": str(completed.stdout or "").strip().splitlines()[-3:],
        "stderr_tail": str(completed.stderr or "").strip().splitlines()[-3:],
    }


def build_report() -> dict[str, Any]:
    pytest_result = _run_targeted_pytest()
    self_audit = _run_self_audit()
    overall_pass = (
        pytest_result["pass"]
        and self_audit["returncode"] == 0
        and not self_audit["parse_error"]
        and self_audit["phase3_matrix_green"]
        and self_audit["direct_script_imports_eliminated"]
        and self_audit["bootstrap_reduced_from_baseline"]
    )
    return {
        "schema_version": "agn.validation.phase3_script_debt_reduction.v1",
        "generated_at": utc_now_iso(),
        "objective": "Verify that Phase 2 script-debt reduction removed direct scripts imports from real package modules, lowered scripts bootstrap pressure, and preserved hot-path behavior.",
        "baseline": {
            "reference_report": BASELINE_REPORT,
            "direct_script_import_count": BASELINE_DIRECT_SCRIPT_IMPORTS,
            "scripts_bootstrap_count": BASELINE_SCRIPTS_BOOTSTRAP,
        },
        "checks": {
            "targeted_pytest": pytest_result,
            "self_audit": self_audit,
        },
        "binary_verdict": {
            "targeted_tests_green": bool(pytest_result["pass"]),
            "phase3_matrix_green": bool(self_audit["phase3_matrix_green"]),
            "direct_script_imports_eliminated": bool(self_audit["direct_script_imports_eliminated"]),
            "scripts_bootstrap_reduced": bool(self_audit["bootstrap_reduced_from_baseline"]),
            "overall_pass": bool(overall_pass),
        },
    }


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = build_report()
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = REPORT_DIR / f"{stamp}-phase3-script-debt-reduction-acceptance.json"
    latest_path = REPORT_DIR / "phase3-script-debt-reduction-acceptance.latest.json"
    atomic_write_json(report_path, payload)
    atomic_write_json(latest_path, payload)
    print(json.dumps({**payload, "report_path": str(report_path)}, ensure_ascii=True, indent=2))
    return 0 if payload["binary_verdict"]["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
