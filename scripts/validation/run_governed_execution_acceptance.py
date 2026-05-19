#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from admin_control_common import atomic_write_json
from agn_governed_execution import dispatch_desktop_action, dispatch_memory_record
from capability_snapshot import build_capability_snapshot
from agn_infrastructure_map import recommend_modules

REPORT_DIR = ROOT / "reports" / "validation"
TEST_COMMAND = [
    "uvx",
    "pytest",
    "tests/test_agn_governed_execution.py",
    "tests/test_dispatcher_runtime.py",
    "tests/test_agn_visual_operator.py",
    "tests/test_agn_artifact_bridge.py",
    "tests/test_agn_tool.py",
    "tests/test_desktop_adapter.py",
    "tests/test_agn_infrastructure_map.py",
    "tests/test_capability_snapshot.py",
    "tests/test_control_plane_read_model.py",
    "-q",
]
ACTIVE_FILES = [
    "scripts/agn_visual_operator.py",
    "scripts/agn_artifact_bridge.py",
    "scripts/agn_tool.py",
    "scripts/agn_mcp_server.py",
    "scripts/agent_collaboration.py",
    "scripts/research_llm.py",
]
FORBIDDEN_PATTERNS = [
    r"from\s+desktop_adapter\s+import\s+run_desktop_action",
    r"from\s+scripts\.desktop_adapter\s+import\s+run_desktop_action",
    r"from\s+memory_recorder\s+import\s+append_record",
    r"from\s+scripts\.memory_recorder\s+import\s+append_record",
    r"from\s+model_router\s+import\s+run_routed_task",
    r"from\s+scripts\.model_router\s+import\s+run_routed_task",
    r"from\s+review_orchestrator\s+import\s+run_review",
    r"from\s+scripts\.review_orchestrator\s+import\s+run_review",
    r"from\s+vision_parser\s+import\s+parse_vision_ref",
    r"from\s+scripts\.vision_parser\s+import\s+parse_vision_ref",
]


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _scan_active_surfaces() -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    compiled = [re.compile(pattern) for pattern in FORBIDDEN_PATTERNS]
    for relative in ACTIVE_FILES:
        path = ROOT / relative
        text = path.read_text(encoding="utf-8")
        for pattern in compiled:
            if pattern.search(text):
                findings.append({"file": relative, "pattern": pattern.pattern})
    return {
        "files_checked": len(ACTIVE_FILES),
        "forbidden_pattern_count": len(FORBIDDEN_PATTERNS),
        "finding_count": len(findings),
        "findings": findings,
        "pass": len(findings) == 0,
    }


def _run_tests() -> dict[str, Any]:
    completed = subprocess.run(
        TEST_COMMAND,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=180.0,
        check=False,
    )
    stdout = str(completed.stdout or "")
    summary_match = re.search(r"(\d+)\s+passed", stdout)
    passed_count = int(summary_match.group(1)) if summary_match else 0
    failed_count = 0 if completed.returncode == 0 else 1
    return {
        "command": TEST_COMMAND,
        "returncode": int(completed.returncode),
        "passed_count": passed_count,
        "failed_count": failed_count,
        "stdout_tail": stdout.strip().splitlines()[-5:],
        "stderr_tail": str(completed.stderr or "").strip().splitlines()[-5:],
        "pass": completed.returncode == 0,
    }


def _live_checks() -> dict[str, Any]:
    memory = dispatch_memory_record(
        {
            "kind": "status",
            "summary": "Phase 2 governed execution acceptance check",
            "scope": "test",
            "author": "phase2-acceptance",
            "confidence": "high",
        },
        caller="phase2_acceptance",
        task_id="phase2-governed-execution-memory",
        trace_id="trace-phase2-governed-execution-memory",
        intent="record_acceptance_status",
        reason="Phase 2 governed execution live acceptance memory append",
        risk_level="low",
    )
    desktop = dispatch_desktop_action(
        {
            "action_type": "DESKTOP_OBSERVE",
            "params": {"surface": "status"},
        },
        caller="phase2_acceptance",
        task_id="phase2-governed-execution-desktop",
        trace_id="trace-phase2-governed-execution-desktop",
        intent="desktop_observe_status",
        reason="Phase 2 governed execution live acceptance desktop observe",
        risk_level="low",
    )
    return {
        "memory_dispatch": {
            "ok": bool(memory.get("ok", False)),
            "record_id": str((memory.get("record", {}) or {}).get("record_id", "")),
            "dispatch_meta": memory.get("dispatch_meta", {}),
            "error": str(memory.get("error", "")),
        },
        "desktop_dispatch": {
            "ok": bool(desktop.get("ok", False)),
            "result_surface": str(
                (desktop.get("result", {}) or {}).get("surface", "")
                or ((desktop.get("result", {}) or {}).get("stdout", {}) or {}).get("surface", "")
            ),
            "dispatch_meta": desktop.get("dispatch_meta", {}),
            "error": str(desktop.get("error", "")),
        },
    }


def _architecture_checks() -> dict[str, Any]:
    capability = build_capability_snapshot()
    recommendation = recommend_modules(task_summary="Continue the AGN restructuring, integrate a new GitHub browser automation repo, and inspect Twitter AI news in Chrome.")
    module_ids = [str(item.get("module_id", "")).strip() for item in recommendation.get("recommendations", []) if isinstance(item, dict)]
    return {
        "gateway_surface_available": bool(((capability.get("surfaces", {}) or {}).get("governed_execution_gateway", {}) or {}).get("available", False)),
        "gateway_in_recommendations": "governed_execution_gateway" in module_ids,
        "recommended_module_ids": module_ids,
    }


def build_acceptance_report() -> dict[str, Any]:
    scan = _scan_active_surfaces()
    tests = _run_tests()
    live = _live_checks()
    architecture = _architecture_checks()
    overall_pass = all(
        [
            scan["pass"],
            tests["pass"],
            live["memory_dispatch"]["ok"],
            live["desktop_dispatch"]["ok"],
            architecture["gateway_surface_available"],
            architecture["gateway_in_recommendations"],
        ]
    )
    return {
        "schema_version": "agn.validation.governed_execution_acceptance.v1",
        "generated_at": utc_now_iso(),
        "objective": "Verify that active Phase 2 execution surfaces route through the governed execution gateway instead of direct handler imports.",
        "binary_verdict": {
            "active_surfaces_no_forbidden_imports": bool(scan["pass"]),
            "targeted_tests_green": bool(tests["pass"]),
            "live_memory_dispatch_ok": bool(live["memory_dispatch"]["ok"]),
            "live_desktop_dispatch_ok": bool(live["desktop_dispatch"]["ok"]),
            "gateway_surface_available": bool(architecture["gateway_surface_available"]),
            "gateway_recommended_for_reconstruction_task": bool(architecture["gateway_in_recommendations"]),
            "overall_pass": bool(overall_pass),
        },
        "counts": {
            "files_checked": int(scan["files_checked"]),
            "forbidden_findings": int(scan["finding_count"]),
            "tests_passed": int(tests["passed_count"]),
            "tests_failed": int(tests["failed_count"]),
            "live_checks_passed": int(sum(1 for item in (live["memory_dispatch"], live["desktop_dispatch"]) if item["ok"])),
        },
        "static_scan": scan,
        "test_run": tests,
        "live_checks": live,
        "architecture_checks": architecture,
    }


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = build_acceptance_report()
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = REPORT_DIR / f"{timestamp}-governed-execution-acceptance.json"
    latest = REPORT_DIR / "governed-execution-acceptance.latest.json"
    atomic_write_json(report_path, payload)
    atomic_write_json(latest, payload)
    print(json.dumps({**payload, "report_path": str(report_path)}, ensure_ascii=True, indent=2))
    return 0 if payload["binary_verdict"]["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
