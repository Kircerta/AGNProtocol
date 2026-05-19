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
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from admin_control_common import atomic_write_json
from provider_registry import probe_capabilities
from agn.runtime.host_info import build_host_info


REPORT_DIR = ROOT / "reports" / "validation"
REAL_IMPL_MARKERS = (
    "This is the real package implementation",
    "This module holds the real implementation",
)
SCRIPT_IMPORT_RE = re.compile(r"(?:from|import)\s+scripts\.")


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _is_real_module(text: str) -> bool:
    return any(marker in text for marker in REAL_IMPL_MARKERS)


def _is_proxy_module(text: str) -> bool:
    return " import *" in text and not _is_real_module(text)


def _inventory() -> dict[str, Any]:
    scripts_root = ROOT / "scripts"
    src_root = ROOT / "src" / "agn"

    scripts_top = sorted(scripts_root.glob("*.py"))
    scripts_recursive = sorted(scripts_root.rglob("*.py"))
    src_modules = sorted(src_root.rglob("*.py"))

    real_modules: list[dict[str, Any]] = []
    proxy_modules: list[str] = []
    other_modules: list[str] = []
    for path in src_modules:
        rel = str(path.relative_to(src_root))
        text = path.read_text(encoding="utf-8")
        if _is_real_module(text):
            real_modules.append(
                {
                    "module": rel,
                    "script_import_refs": len(SCRIPT_IMPORT_RE.findall(text)),
                    "adds_scripts_to_path": 'str(ROOT / "scripts")' in text,
                }
            )
        elif _is_proxy_module(text):
            proxy_modules.append(rel)
        else:
            other_modules.append(rel)

    return {
        "scripts_top_level_py": len(scripts_top),
        "scripts_recursive_py": len(scripts_recursive),
        "src_agn_py_total": len(src_modules),
        "src_agn_real": len(real_modules),
        "src_agn_proxy": len(proxy_modules),
        "src_agn_other": len(other_modules),
        "remaining_proxy_modules": proxy_modules,
        "real_modules_with_direct_script_imports": [item for item in real_modules if item["script_import_refs"] > 0],
        "real_modules_with_scripts_path_bootstrap": [item for item in real_modules if item["adds_scripts_to_path"]],
    }


def _run(cmd: list[str], *, timeout_sec: float = 300.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=timeout_sec,
        check=False,
    )


def _phase3_matrix() -> dict[str, Any]:
    scripts = sorted((ROOT / "scripts" / "validation").glob("run_phase3_*migration_acceptance.py"))
    results: list[dict[str, Any]] = []
    for script in scripts:
        completed = _run([sys.executable, str(script)])
        payload: dict[str, Any] | None = None
        try:
            payload = json.loads(str(completed.stdout or "{}"))
        except json.JSONDecodeError:
            payload = None
        result = {
            "script": script.name,
            "returncode": int(completed.returncode),
            "overall_pass": bool(payload.get("binary_verdict", {}).get("overall_pass")) if isinstance(payload, dict) else False,
            "report_path": str(payload.get("report_path", "")) if isinstance(payload, dict) else "",
            "stdout_tail": str(completed.stdout or "").strip().splitlines()[-3:],
            "stderr_tail": str(completed.stderr or "").strip().splitlines()[-3:],
        }
        results.append(result)
    failed = [item for item in results if item["returncode"] != 0]
    return {
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": failed,
        "results": results,
    }


def _provider_consistency() -> dict[str, Any]:
    capabilities = probe_capabilities()
    host_info = build_host_info(task_summary="phase3 self-audit provider consistency")
    host_unavailable = {
        str(item.get("name", "")).strip(): str(item.get("reason", "")).strip()
        for item in host_info.get("dependencies", {}).get("providers", {}).get("unavailable", [])
        if isinstance(item, dict)
    }
    host_available = {str(item).strip() for item in host_info.get("dependencies", {}).get("providers", {}).get("available", [])}

    checks: list[dict[str, Any]] = []
    for name in ("qwen_local", "vertex_local"):
        provider = (capabilities.get("executors", {}) or {}).get(name, {})
        probe_available = bool(provider.get("available"))
        host_says_available = name in host_available
        consistent = probe_available == host_says_available
        checks.append(
            {
                "name": name,
                "provider_probe_available": probe_available,
                "provider_probe_reason": str(provider.get("unavailable_reason", "")).strip(),
                "host_info_available": host_says_available,
                "host_info_reason": host_unavailable.get(name, ""),
                "consistent": consistent,
            }
        )
    return {
        "checks": checks,
        "all_consistent": all(item["consistent"] for item in checks),
    }


def build_report() -> dict[str, Any]:
    inventory = _inventory()
    matrix = _phase3_matrix()
    provider_consistency = _provider_consistency()

    fully_systemized = (
        inventory["src_agn_proxy"] == 0
        and not inventory["real_modules_with_direct_script_imports"]
        and not inventory["real_modules_with_scripts_path_bootstrap"]
    )
    overall_pass = matrix["passed"] == matrix["total"] and provider_consistency["all_consistent"]

    findings: list[dict[str, Any]] = []
    if inventory["src_agn_proxy"] > 0:
        findings.append(
            {
                "severity": "medium",
                "title": "Package migration is still incomplete",
                "detail": f"{inventory['src_agn_proxy']} proxy modules remain under src/agn, so AGN is not yet fully package-native.",
            }
        )
    if inventory["real_modules_with_direct_script_imports"]:
        findings.append(
            {
                "severity": "medium",
                "title": "Several real package modules still depend directly on scripts/",
                "detail": f"{len(inventory['real_modules_with_direct_script_imports'])} real package modules still import scripts directly.",
            }
        )
    if not fully_systemized:
        findings.append(
            {
                "severity": "medium",
                "title": "AGN is systemizing, but not fully systemized yet",
                "detail": "Hot-path governance surfaces have migrated, but dispatch and handler layers still remain proxy-backed and some package modules still bootstrap scripts/.",
            }
        )

    return {
        "schema_version": "agn.validation.phase3_self_audit.v1",
        "generated_at": utc_now_iso(),
        "objective": "Audit whether AGN is structurally systemized after recent Phase 3 migrations, measure remaining script debt, and verify that current migration claims still pass real acceptance checks.",
        "binary_verdict": {
            "phase3_matrix_green": matrix["passed"] == matrix["total"],
            "local_provider_consistency_green": bool(provider_consistency["all_consistent"]),
            "fully_systemized": bool(fully_systemized),
            "overall_pass": bool(overall_pass),
        },
        "counts": {
            "scripts_top_level_py": int(inventory["scripts_top_level_py"]),
            "scripts_recursive_py": int(inventory["scripts_recursive_py"]),
            "src_agn_real": int(inventory["src_agn_real"]),
            "src_agn_proxy": int(inventory["src_agn_proxy"]),
            "src_agn_other": int(inventory["src_agn_other"]),
            "phase3_acceptance_total": int(matrix["total"]),
            "phase3_acceptance_passed": int(matrix["passed"]),
        },
        "inventory": inventory,
        "phase3_matrix": matrix,
        "provider_consistency": provider_consistency,
        "findings": findings,
    }


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = build_report()
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = REPORT_DIR / f"{timestamp}-phase3-self-audit.json"
    latest = REPORT_DIR / "phase3-self-audit.latest.json"
    atomic_write_json(report_path, payload)
    atomic_write_json(latest, payload)
    print(json.dumps({**payload, "report_path": str(report_path)}, ensure_ascii=True, indent=2))
    return 0 if payload["binary_verdict"]["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
