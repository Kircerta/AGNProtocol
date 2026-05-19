"""AGN task-start kernel surface.

This is the real package implementation for AGN's task-start aggregation
module. The legacy script remains as a compatibility shim.
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
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from agn.core.admin_control import atomic_write_json

from agn.runtime.host_info import build_host_info

try:
    from agn_memory_recall import query_memory_recall
except ImportError:  # pragma: no cover
    from agn_memory_recall import query_memory_recall


PACKAGE_PATH = "agn.governance.task_start_kernel"
LEGACY_SCRIPT_SHIM = "scripts/agn_task_start_kernel.py"


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _provider_summary(snapshot: dict[str, Any]) -> dict[str, bool]:
    payload = snapshot.get("provider_summary", {}) if isinstance(snapshot.get("provider_summary"), dict) else {}
    return {
        "qwen_local": bool(payload.get("qwen_local")),
        "deepseek": bool(payload.get("deepseek")),
        "gemini": bool(payload.get("gemini")),
        "claude": bool(payload.get("claude")),
    }


def _provider_summary_from_host_info(host_info: dict[str, Any]) -> dict[str, bool]:
    available = host_info.get("dependencies", {}).get("providers", {}).get("available", [])
    names = {str(item).strip().lower() for item in available if str(item).strip()}
    return {
        "qwen_local": "qwen_local" in names,
        "deepseek": "deepseek" in names,
        "gemini": "gemini" in names,
        "claude": "claude" in names,
    }


def _kernel_status(*, system_mode: dict[str, Any], lifecycle: dict[str, Any], host_info: dict[str, Any], provider_summary: dict[str, bool]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    mode = str(system_mode.get("mode", "")).strip().lower()
    lifecycle_status = str(lifecycle.get("status", "")).strip().lower()
    host_readiness = str(host_info.get("task_readiness", {}).get("status", "")).strip().lower()

    if mode == "emergency_stop":
        reasons.append("system_mode_emergency_stop")
        return "blocked", reasons
    if lifecycle_status and lifecycle_status != "running":
        reasons.append(f"lifecycle_{lifecycle_status}")
    if host_readiness and host_readiness != "ready":
        reasons.append(f"host_{host_readiness}")
    if not any(bool(value) for value in provider_summary.values()):
        reasons.append("no_provider_available")
    if reasons:
        return "attention", reasons
    return "ready", ["runtime_and_host_aligned"]


def build_task_start_kernel(
    *,
    task_summary: str,
    risk_level: str,
    snapshot: dict[str, Any] | None = None,
    needs_desktop: bool = False,
    refresh_host_info: bool = False,
) -> dict[str, Any]:
    snapshot_payload = snapshot if isinstance(snapshot, dict) else {}
    system_mode = snapshot_payload.get("system_mode", {}) if isinstance(snapshot_payload.get("system_mode"), dict) else {}
    lifecycle = snapshot_payload.get("lifecycle", {}) if isinstance(snapshot_payload.get("lifecycle"), dict) else {}
    host_info = build_host_info(task_summary=task_summary, refresh=refresh_host_info)
    provider_summary = _provider_summary(snapshot_payload)
    host_provider_summary = _provider_summary_from_host_info(host_info)
    provider_summary = {name: bool(provider_summary.get(name) or host_provider_summary.get(name)) for name in provider_summary}
    available_providers = [name for name, available in sorted(provider_summary.items()) if bool(available)]
    memory_recall = query_memory_recall(
        task_summary=task_summary,
        tools=(["browser-use"] if needs_desktop else []),
        providers=available_providers,
    )
    tool_reality_cards = memory_recall.get("tool_reality_cards", []) if isinstance(memory_recall.get("tool_reality_cards"), list) else []
    status, status_reasons = _kernel_status(
        system_mode=system_mode,
        lifecycle=lifecycle,
        host_info=host_info,
        provider_summary=provider_summary,
    )

    return {
        "schema_version": "agn.task_start_kernel.v1",
        "generated_at": utc_now_iso(),
        "package_path": PACKAGE_PATH,
        "legacy_script_shim": LEGACY_SCRIPT_SHIM,
        "task_summary": str(task_summary).strip(),
        "risk_level": str(risk_level).strip().lower(),
        "needs_desktop": bool(needs_desktop),
        "runtime_snapshot": {
            "system_mode": str(system_mode.get("mode", "")).strip() or "unknown",
            "lifecycle_status": str(lifecycle.get("status", "")).strip() or "unknown",
            "provider_summary": provider_summary,
        },
        "host_info": host_info,
        "memory_recall": memory_recall,
        "tool_reality_cards": tool_reality_cards,
        "summary": {
            "status": status,
            "status_reasons": status_reasons,
            "host_readiness": str(host_info.get("task_readiness", {}).get("status", "")).strip() or "unknown",
            "host_freshness": str(host_info.get("freshness", {}).get("status", "")).strip() or "unknown",
            "provider_count": len(available_providers),
            "memory_prior_count": len(memory_recall.get("priors", [])) if isinstance(memory_recall.get("priors"), list) else 0,
            "tool_reality_card_count": len(tool_reality_cards),
        },
    }


def write_task_start_kernel(payload: dict[str, Any], *, output_path: Path) -> Path:
    atomic_write_json(output_path, payload)
    return output_path


def cmd_build(args: argparse.Namespace) -> int:
    from agn.governance.execution_workflow import system_snapshot

    payload = build_task_start_kernel(
        task_summary=str(args.task_summary).strip(),
        risk_level=str(args.risk_level).strip().lower(),
        snapshot=system_snapshot(),
        needs_desktop=bool(args.needs_desktop),
        refresh_host_info=bool(args.refresh_host_info),
    )
    if args.output:
        write_task_start_kernel(payload, output_path=Path(args.output).expanduser().resolve())
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build AGN's task-start kernel: local host facts, memory priors, and tool reality cards for one task.")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="Build the task-start kernel for a task summary.")
    build.add_argument("--task-summary", required=True)
    build.add_argument("--risk-level", default="medium")
    build.add_argument("--needs-desktop", action="store_true")
    build.add_argument("--refresh-host-info", action="store_true")
    build.add_argument("--output", default="")
    build.set_defaults(func=cmd_build)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
