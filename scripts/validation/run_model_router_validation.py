#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from model_router import build_route_decision, run_routed_task
from provider_registry import load_registry, probe_capabilities


REPORT_PATH = ROOT / "reports" / "model_router_validation.json"
RUNTIME_DIR = ROOT / "runtime" / "model_router_examples"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    capabilities = probe_capabilities(load_registry())
    qwen_available = bool(capabilities.get("executors", {}).get("qwen_local", {}).get("available"))
    deepseek_available = bool(capabilities.get("reviewers", {}).get("deepseek", {}).get("available"))
    gemini_cli_available = bool(capabilities.get("executors", {}).get("gemini", {}).get("available"))
    gemini_credentials_present = bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))
    gemini_runnable = gemini_cli_available and gemini_credentials_present
    low_risk_task = {
        "task_id": "model-router-low-risk",
        "task_type": "json_extraction",
        "prompt": (
            "Extract invoice_id, customer, due_date, amount_usd, and paid from this note.\n"
            "Text: INV 2048 // Acme Labs // due Mar 20 2026 // amount USD 1284.50 // unpaid"
        ),
        "response_mode": "json_object",
        "json_schema": {
            "type": "object",
            "required": ["invoice_id", "customer", "due_date", "amount_usd", "paid"],
            "properties": {
                "invoice_id": {"type": "string"},
                "customer": {"type": "string"},
                "due_date": {"type": "string"},
                "amount_usd": {"type": "number"},
                "paid": {"type": "boolean"},
            },
        },
        "risk_level": "low",
        "logical_complexity": "low",
        "verification_cost": "low",
        "cost_sensitivity": "high",
    }
    complex_task = {
        "task_id": "model-router-complex-dry-run",
        "task_type": "complex_reasoning",
        "prompt": "Compare two competing designs for long-term distributed lease recovery and identify hidden failure modes.",
        "response_mode": "text",
        "risk_level": "high",
        "logical_complexity": "very_high",
        "verification_cost": "high",
        "cost_sensitivity": "medium",
    }

    qwen_output = RUNTIME_DIR / "low_risk_qwen_local.json"
    deepseek_output = RUNTIME_DIR / "low_risk_deepseek.json"
    gemini_output = RUNTIME_DIR / "low_risk_gemini_flash.json"
    auto_output = RUNTIME_DIR / "low_risk_auto.json"
    fallback_output = RUNTIME_DIR / "low_risk_forced_fallback.json"

    qwen_result = (
        run_routed_task(low_risk_task, output_path=qwen_output, forced_provider="qwen_local")
        if qwen_available
        else {"ok": True, "skipped": True, "reason": capabilities.get("executors", {}).get("qwen_local", {}).get("unavailable_reason", "qwen_local_unavailable")}
    )
    deepseek_result = (
        run_routed_task(low_risk_task, output_path=deepseek_output, forced_provider="deepseek")
        if deepseek_available
        else {"ok": False, "skipped": True, "reason": capabilities.get("reviewers", {}).get("deepseek", {}).get("unavailable_reason", "deepseek_unavailable")}
    )
    gemini_result = (
        run_routed_task(low_risk_task, output_path=gemini_output, forced_provider="gemini")
        if gemini_runnable
        else {"ok": True, "skipped": True, "reason": "gemini_credentials_missing" if gemini_cli_available else "gemini_cli_unavailable"}
    )
    auto_result = run_routed_task(low_risk_task, output_path=auto_output)

    original_qwen = os.environ.get("QWEN_LOCAL_BASE_URL")
    os.environ["QWEN_LOCAL_BASE_URL"] = "http://127.0.0.1:9876/v1"
    try:
        fallback_result = run_routed_task(low_risk_task, output_path=fallback_output)
    finally:
        if original_qwen is None:
            os.environ.pop("QWEN_LOCAL_BASE_URL", None)
        else:
            os.environ["QWEN_LOCAL_BASE_URL"] = original_qwen

    dry_run_decision = build_route_decision(complex_task)
    qwen_record = qwen_result.get("result", {}).get("parsed", {})
    gemini_record = gemini_result.get("result", {}).get("parsed", {})
    deepseek_record = deepseek_result.get("result", {}).get("parsed", {})
    comparable_records = [
        record
        for record in (qwen_record, deepseek_record, gemini_record)
        if isinstance(record, dict) and record
    ]
    same_top_level_keys = bool(comparable_records) and all(sorted(record.keys()) == sorted(comparable_records[0].keys()) for record in comparable_records)

    summary = {
        "ok": bool(qwen_result.get("ok") and deepseek_result.get("ok") and gemini_result.get("ok") and auto_result.get("ok") and fallback_result.get("ok")),
        "tests": {
            "same_task_compare": {
                "qwen_ok": bool(qwen_result.get("ok")),
                "qwen_skipped": bool(qwen_result.get("skipped")),
                "qwen_skip_reason": str(qwen_result.get("reason", "")),
                "deepseek_ok": bool(deepseek_result.get("ok")),
                "gemini_ok": bool(gemini_result.get("ok")),
                "gemini_skipped": bool(gemini_result.get("skipped")),
                "gemini_skip_reason": str(gemini_result.get("reason", "")),
                "qwen_provider": str(qwen_result.get("route_decision", {}).get("selected_provider", "")),
                "deepseek_provider": str(deepseek_result.get("route_decision", {}).get("selected_provider", "")),
                "gemini_provider": str(gemini_result.get("route_decision", {}).get("selected_provider", "")),
                "gemini_model": str(gemini_result.get("attempts", [{}])[-1].get("model_name", "")) if gemini_result.get("attempts") else "",
                "qwen_keys": sorted(qwen_record.keys()) if isinstance(qwen_record, dict) else [],
                "deepseek_keys": sorted(deepseek_record.keys()) if isinstance(deepseek_record, dict) else [],
                "gemini_keys": sorted(gemini_record.keys()) if isinstance(gemini_record, dict) else [],
                "same_top_level_keys": same_top_level_keys,
            },
            "auto_route": {
                "ok": bool(auto_result.get("ok")),
                "selected_provider": str(auto_result.get("route_decision", {}).get("selected_provider", "")),
                "candidate_chain": [str(item.get("provider", "")) for item in auto_result.get("route_decision", {}).get("candidate_chain", [])],
            },
            "fallback_route": {
                "ok": bool(fallback_result.get("ok")),
                "selected_provider": str(fallback_result.get("route_decision", {}).get("selected_provider", "")),
                "fallback_from": str(fallback_result.get("route_decision", {}).get("fallback_from", "")),
                "attempted_providers": [str(item.get("provider", "")) for item in fallback_result.get("attempts", [])],
            },
            "dry_run_complex_policy": {
                "selected_provider": str(dry_run_decision.get("selected_provider", "")),
                "filtered_providers": dry_run_decision.get("filtered_providers", []),
            },
        },
        "artifacts": {
            "qwen_output": str(qwen_output),
            "deepseek_output": str(deepseek_output),
            "gemini_output": str(gemini_output),
            "auto_output": str(auto_output),
            "fallback_output": str(fallback_output),
            "report": str(REPORT_PATH),
        },
    }
    _write_json(REPORT_PATH, summary)
    print(json.dumps(summary, ensure_ascii=True, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
