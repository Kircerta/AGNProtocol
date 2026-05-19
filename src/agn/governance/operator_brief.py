"""AGN operator brief surface.

This is the real package implementation for AGN's low-noise task-start
operator brief. The legacy script remains as a compatibility shim.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


PACKAGE_PATH = "agn.governance.operator_brief"
LEGACY_SCRIPT_SHIM = "scripts/agn_operator_brief.py"


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _detail_item(*, source: str, title: str, detail: str, check: str = "", status: str = "") -> dict[str, str]:
    payload = {
        "source": source,
        "title": str(title or "").strip(),
        "detail": str(detail or "").strip(),
    }
    if check:
        payload["check"] = str(check).strip()
    if status:
        payload["status"] = str(status).strip()
    return payload


def build_operator_brief(
    *,
    task_summary: str,
    risk_level: str,
    system_snapshot: dict[str, Any],
    execution_checks: list[dict[str, Any]],
    task_start_kernel: dict[str, Any],
    recommended_surfaces: list[dict[str, Any]],
) -> dict[str, Any]:
    blocking_items: list[dict[str, str]] = []
    attention_items: list[dict[str, str]] = []
    informational_items: list[dict[str, str]] = []

    for item in execution_checks:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "")).strip()
        detail = str(item.get("detail", "")).strip()
        check = str(item.get("check", "")).strip()
        if status == "blocked":
            blocking_items.append(_detail_item(source="execution_check", check=check, status=status, title=check, detail=detail))
        elif status == "attention" and check != "memory_recall":
            attention_items.append(_detail_item(source="execution_check", check=check, status=status, title=check, detail=detail))

    lifecycle = system_snapshot.get("lifecycle", {}) if isinstance(system_snapshot.get("lifecycle"), dict) else {}
    lifecycle_status = str(lifecycle.get("status", "")).strip().lower()
    if lifecycle_status and lifecycle_status != "running":
        attention_items.append(
            _detail_item(
                source="runtime",
                title="lifecycle_refresh",
                detail=f"Lifecycle is {lifecycle_status}; refresh or start AGN before trusting stale read models.",
            )
        )

    kernel_summary = task_start_kernel.get("summary", {}) if isinstance(task_start_kernel.get("summary"), dict) else {}
    memory_recall = task_start_kernel.get("memory_recall", {}) if isinstance(task_start_kernel.get("memory_recall"), dict) else {}
    host_info = task_start_kernel.get("host_info", {}) if isinstance(task_start_kernel.get("host_info"), dict) else {}
    recall_priors = memory_recall.get("priors", []) if isinstance(memory_recall.get("priors"), list) else []
    recall_cards = task_start_kernel.get("tool_reality_cards", []) if isinstance(task_start_kernel.get("tool_reality_cards"), list) else []
    informational_items.append(
        _detail_item(
            source="task_start_kernel",
            title="task_start_kernel",
            detail=(
                f"Kernel consulted {len(recall_priors)} priors and {len(recall_cards)} tool reality cards. "
                f"Host readiness is {kernel_summary.get('host_readiness', 'unknown')} and runtime facts still win."
            ),
        )
    )

    host_identity = host_info.get("host_identity", {}) if isinstance(host_info.get("host_identity"), dict) else {}
    freshness = host_info.get("freshness", {}) if isinstance(host_info.get("freshness"), dict) else {}
    task_readiness = host_info.get("task_readiness", {}) if isinstance(task_start_kernel.get("host_info"), dict) else {}
    current_host_id = str(host_identity.get("host_id", "")).strip()
    freshness_status = str(freshness.get("status", "")).strip() or "unknown"
    readiness_status = str(task_readiness.get("status", "")).strip() or "attention"

    if freshness_status == "fresh" and readiness_status == "ready" and current_host_id:
        informational_items.append(
            _detail_item(
                source="host_info",
                title="current_host_ready",
                detail=f"Current host {current_host_id} is fresh and locally ready for this task.",
            )
        )

    top_surfaces = []
    for item in recommended_surfaces[:3]:
        if not isinstance(item, dict):
            continue
        top_surfaces.append(
            {
                "surface": str(item.get("surface", "")).strip(),
                "reason": str(item.get("reason", "")).strip(),
                "entry": str(item.get("entry", "")).strip(),
            }
        )

    if blocking_items:
        status = "blocked"
        summary = f"{len(blocking_items)} blocking issue(s) need resolution before trusted execution."
        next_best_step = top_surfaces[0]["entry"] if top_surfaces else "Resolve the blocking checks before continuing."
    elif attention_items:
        status = "attention"
        summary = f"{len(attention_items)} attention item(s) remain, but the task can still proceed deliberately."
        next_best_step = top_surfaces[0]["entry"] if top_surfaces else "Inspect the attention items and proceed deliberately."
    else:
        status = "ready"
        summary = "No blocking or attention items remain. Runtime facts, memory priors, and local host info are aligned for deliberate execution."
        next_best_step = top_surfaces[0]["entry"] if top_surfaces else "Proceed with the chosen governed execution surface."

    return {
        "schema_version": "agn.operator_brief.v1",
        "generated_at": utc_now_iso(),
        "package_path": PACKAGE_PATH,
        "legacy_script_shim": LEGACY_SCRIPT_SHIM,
        "task_summary": str(task_summary).strip(),
        "risk_level": str(risk_level).strip(),
        "status": status,
        "summary": summary,
        "counts": {
            "blocking": len(blocking_items),
            "attention": len(attention_items),
            "informational": len(informational_items),
        },
        "blocking_items": blocking_items,
        "attention_items": attention_items,
        "informational_items": informational_items,
        "top_surfaces": top_surfaces,
        "next_best_step": next_best_step,
    }


def cmd_build(args: argparse.Namespace) -> int:
    from agn.governance.execution_workflow import build_preflight_payload

    risk_level = str(args.risk_level or "medium").strip().lower() or "medium"
    preflight = build_preflight_payload(
        task_summary=str(args.task_summary).strip(),
        risk_level=risk_level,
        task_id="operator-brief-preview",
        trace_id="trace-operator-brief-preview",
        subsystem="agn2",
        needs_control_plane=bool(args.needs_control_plane),
        needs_desktop=bool(args.needs_desktop),
        needs_history=bool(args.needs_history),
        needs_worker=bool(args.needs_worker),
        needs_review=bool(args.needs_review),
    )
    print(json.dumps(preflight.get("operator_brief", {}), ensure_ascii=True, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a low-noise operator brief for an AGN task start.")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="Build an operator brief from a live preflight.")
    build.add_argument("--task-summary", required=True)
    build.add_argument("--risk-level", default="medium")
    build.add_argument("--needs-control-plane", action="store_true")
    build.add_argument("--needs-desktop", action="store_true")
    build.add_argument("--needs-history", action="store_true")
    build.add_argument("--needs-worker", action="store_true")
    build.add_argument("--needs-review", action="store_true")
    build.set_defaults(func=cmd_build)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
