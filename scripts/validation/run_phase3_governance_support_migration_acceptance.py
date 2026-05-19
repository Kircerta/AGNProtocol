#!/usr/bin/env python3
from __future__ import annotations

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
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from admin_control_common import atomic_write_json


REPORT_DIR = ROOT / "reports" / "validation"


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )


def _tail(text: str, *, limit: int = 1200) -> str:
    raw = str(text or "")
    return raw[-limit:]


def _pytest_checks() -> dict[str, Any]:
    cmd = [
        "uvx",
        "pytest",
        "-q",
        "tests/test_package_review_contract.py",
        "tests/test_package_council.py",
        "tests/test_package_bridge.py",
        "tests/test_package_lifecycle.py",
        "tests/test_package_visual_security.py",
        "tests/test_council_review.py",
        "tests/test_evo5_lifecycle_governance.py",
    ]
    completed = _run(cmd)
    return {
        "command": cmd,
        "returncode": int(completed.returncode),
        "stdout_tail": _tail(completed.stdout),
        "stderr_tail": _tail(completed.stderr),
        "pass": int(completed.returncode) == 0,
    }


def _shim_checks() -> dict[str, Any]:
    bridge_script = (ROOT / "scripts" / "agn2_governance_bridge.py").read_text(encoding="utf-8")
    council_script = (ROOT / "scripts" / "council_review.py").read_text(encoding="utf-8")
    lifecycle_script = (ROOT / "scripts" / "lifecycle_governance.py").read_text(encoding="utf-8")
    review_contract_script = (ROOT / "scripts" / "review_contract.py").read_text(encoding="utf-8")
    visual_security_script = (ROOT / "scripts" / "visual_security.py").read_text(encoding="utf-8")
    return {
        "bridge_shim_present": {"pass": "agn.governance" in bridge_script},
        "council_shim_present": {"pass": "agn.governance" in council_script},
        "lifecycle_shim_present": {"pass": "agn.governance" in lifecycle_script},
        "review_contract_shim_present": {"pass": "agn.governance" in review_contract_script},
        "visual_security_shim_present": {"pass": "agn.handlers" in visual_security_script},
    }


def _package_checks() -> dict[str, Any]:
    bridge_text = (ROOT / "src" / "agn" / "governance" / "bridge.py").read_text(encoding="utf-8")
    council_text = (ROOT / "src" / "agn" / "governance" / "council.py").read_text(encoding="utf-8")
    lifecycle_text = (ROOT / "src" / "agn" / "governance" / "lifecycle.py").read_text(encoding="utf-8")
    review_contract_text = (ROOT / "src" / "agn" / "governance" / "review_contract.py").read_text(encoding="utf-8")
    visual_security_text = (ROOT / "src" / "agn" / "handlers" / "visual_security.py").read_text(encoding="utf-8")
    return {
        "bridge_real_impl": {"pass": "proxy for scripts" not in bridge_text and "evaluate_agn1_dispatch" in bridge_text},
        "council_real_impl": {"pass": "proxy for scripts" not in council_text and "aggregate_council_case" in council_text},
        "lifecycle_real_impl": {"pass": "proxy for scripts" not in lifecycle_text and "integrity_sweep" in lifecycle_text},
        "review_contract_real_impl": {"pass": "proxy for scripts" not in review_contract_text and "merge_structured_verdicts" in review_contract_text},
        "visual_security_real_impl": {"pass": "proxy for scripts" not in visual_security_text and "sanitize_ocr_words" in visual_security_text},
    }


def build_report() -> dict[str, Any]:
    pytest_checks = _pytest_checks()
    shim_checks = _shim_checks()
    package_checks = _package_checks()
    checks = {**shim_checks, **package_checks, "pytest_governance_support_suite": pytest_checks}
    passed = sum(1 for value in checks.values() if bool(value.get("pass")))
    total = len(checks)
    overall_pass = passed == total
    return {
        "schema_version": "agn.validation.phase3_governance_support_migration.v1",
        "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z"),
        "objective": "Verify that bridge, council, lifecycle, review contract, and visual security now live in src/agn while legacy script entrypoints remain compatible shims.",
        "binary_verdict": {
            "targeted_tests_green": bool(pytest_checks.get("pass")),
            "script_shims_present": all(bool(item.get("pass")) for item in shim_checks.values()),
            "package_surfaces_real": all(bool(item.get("pass")) for item in package_checks.values()),
            "overall_pass": bool(overall_pass),
        },
        "ok": passed == total,
        "phase": "phase_1_governance_support_proxy_closeout",
        "focus": [
            "agn.governance.bridge",
            "agn.governance.council",
            "agn.governance.lifecycle",
            "agn.governance.review_contract",
            "agn.handlers.visual_security",
        ],
        "checks_passed": passed,
        "checks_total": total,
        "checks": checks,
        "overall_pass": overall_pass,
    }


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = build_report()
    stamp = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORT_DIR / f"{stamp}-phase3-governance-support-migration-acceptance.json"
    atomic_write_json(path, report)
    print(json.dumps({**report, "report_path": str(path)}, ensure_ascii=True, indent=2))
    return 0 if bool(report.get("overall_pass")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
