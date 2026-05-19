"""AGN change-foundry surface.

This is the real package implementation for AGN's evolution pipeline: the
architecture-facing intake, fusion, upgrade, and retirement guidance layer.
The legacy script remains as a compatibility shim.
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
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from agn.core.admin_control import atomic_write_json, read_models_dir
from agn_external_toolbox import build_inventory as build_external_toolbox_inventory

from agn.architecture.infrastructure_map import build_infrastructure_map


PACKAGE_PATH = "agn.architecture.evolution_pipeline"
LEGACY_SCRIPT_SHIM = "scripts/agn_evolution_pipeline.py"
REGISTRY_PATH = ROOT / "config" / "evolution_pipeline_registry.json"
READ_MODEL_PATH = read_models_dir() / "evolution_pipeline.json"


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _registry() -> dict[str, Any]:
    payload = _load_json(REGISTRY_PATH, {})
    return payload if isinstance(payload, dict) else {}


def _module_index() -> dict[str, dict[str, Any]]:
    payload = build_infrastructure_map()
    modules = payload.get("modules", []) if isinstance(payload.get("modules"), list) else []
    return {
        str(item.get("module_id", "")).strip(): item
        for item in modules
        if isinstance(item, dict) and str(item.get("module_id", "")).strip()
    }


def _resolve_touchpoints(touchpoints: list[Any], modules: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in touchpoints:
        module_id = str(raw).strip()
        if not module_id:
            continue
        module = modules.get(module_id, {})
        rows.append(
            {
                "module_id": module_id,
                "display_name": str(module.get("display_name", "")).strip(),
                "district": str(module.get("district", "")).strip(),
                "status": str(module.get("status", "")).strip() or "unknown",
                "available": bool(module.get("available")),
                "resolved_entry": str(module.get("resolved_entry", module.get("entry", ""))).strip(),
            }
        )
    return rows


def build_evolution_pipeline() -> dict[str, Any]:
    registry = _registry()
    toolbox = build_external_toolbox_inventory()
    modules = _module_index()
    pipelines = registry.get("pipelines", []) if isinstance(registry.get("pipelines"), list) else []
    tiers = registry.get("integration_tiers", []) if isinstance(registry.get("integration_tiers"), list) else []
    risks = registry.get("future_risks", []) if isinstance(registry.get("future_risks"), list) else []

    resolved_pipelines: list[dict[str, Any]] = []
    for pipeline in pipelines:
        if not isinstance(pipeline, dict):
            continue
        touchpoints = _resolve_touchpoints(
            pipeline.get("touchpoints", []) if isinstance(pipeline.get("touchpoints"), list) else [],
            modules,
        )
        resolved_pipelines.append(
            {
                "pipeline_id": str(pipeline.get("pipeline_id", "")).strip(),
                "display_name": str(pipeline.get("display_name", "")).strip(),
                "purpose": str(pipeline.get("purpose", "")).strip(),
                "triggers": list(pipeline.get("triggers", [])) if isinstance(pipeline.get("triggers"), list) else [],
                "default_target_tier": str(pipeline.get("default_target_tier", "")).strip(),
                "guardrails": list(pipeline.get("guardrails", [])) if isinstance(pipeline.get("guardrails"), list) else [],
                "stages": list(pipeline.get("stages", [])) if isinstance(pipeline.get("stages"), list) else [],
                "touchpoints": touchpoints,
                "touchpoint_summary": {
                    "count": len(touchpoints),
                    "available_count": sum(1 for item in touchpoints if bool(item.get("available"))),
                    "paused_count": sum(1 for item in touchpoints if str(item.get("status", "")).strip() == "compat_paused"),
                },
            }
        )

    entries = toolbox.get("entries", []) if isinstance(toolbox.get("entries"), list) else []
    mounted = [item for item in entries if isinstance(item, dict)]
    return {
        "schema_version": "agn.evolution_pipeline.v1",
        "generated_at": utc_now_iso(),
        "package_path": PACKAGE_PATH,
        "legacy_script_shim": LEGACY_SCRIPT_SHIM,
        "integration_tiers": tiers,
        "pipelines": resolved_pipelines,
        "future_risks": risks,
        "current_context": {
            "external_toolbox_count": int(toolbox.get("count", 0)),
            "runtime_optional_mounts": sorted(
                str(item.get("name", "")).strip()
                for item in mounted
                if str(item.get("mount_mode", "")).strip() == "runtime_optional"
            ),
            "reference_only_mounts": sorted(
                str(item.get("name", "")).strip()
                for item in mounted
                if str(item.get("mount_mode", "")).strip() == "reference_only"
            ),
            "active_module_count": sum(1 for item in modules.values() if str(item.get("status", "")).strip() == "active"),
            "compat_module_count": sum(1 for item in modules.values() if str(item.get("status", "")).strip() == "compat_paused"),
        },
        "operator_guidance": [
            "New repos should enter through external_intake before any core surgery is proposed.",
            "Promotion should usually climb the tier ladder: reference_only -> toolbox_catalog -> bounded_wrapper -> task_start_support -> first_class_module.",
            "Upgrades must refresh reality cards, priors, and docs so AGN does not keep stale assumptions.",
            "Retirement is part of evolution: pause or archive noisy paths instead of letting them linger half-active.",
        ],
    }


def _normalized_text(*parts: str) -> str:
    return " ".join(str(part or "").strip().lower() for part in parts if str(part or "").strip())


def _recommend_sequence(text: str) -> list[str]:
    retirement_terms = ("archive", "retire", "deprecated", "deprecate", "garbage", "redundant", "cleanup", "pause", "paused", "legacy")
    upgrade_terms = ("upgrade", "update", "refresh", "migrate", "version", "doctor", "dependency", "dependencies")
    intake_terms = ("github", "repo", "repository", "open source", "opensource", "download", "clone", "new tool", "new capability", "new project")
    fusion_terms = ("integrate", "fusion", "fuse", "mount", "wrapper", "skill", "overlay", "reality", "recall", "task-start", "promote", "promotion")

    if any(term in text for term in retirement_terms):
        return ["retirement_and_archive"]

    sequence: list[str] = []
    if any(term in text for term in intake_terms):
        sequence.append("external_intake")
    if any(term in text for term in upgrade_terms):
        sequence.append("governed_upgrade")
    if any(term in text for term in fusion_terms):
        sequence.append("controlled_fusion")
    if not sequence:
        sequence.append("external_intake")
    return sequence


def _recommend_target_tier(text: str, sequence: list[str]) -> str:
    if "retirement_and_archive" in sequence:
        return "paused_legacy"
    if any(term in text for term in ("first-class", "first class", "system module", "core module", "kernel")):
        return "first_class_module"
    if any(term in text for term in ("reality", "recall", "overlay", "task-start", "operator brief", "preflight")):
        return "task_start_support"
    if any(term in text for term in ("wrapper", "browser", "runtime", "automation", "cli", "daemon", "tool", "api")):
        return "bounded_wrapper"
    if any(term in text for term in ("catalog", "discover", "inventory", "toolbox")):
        return "toolbox_catalog"
    if any(term in text for term in ("reference", "prompt", "docs", "readme", "pattern")):
        return "reference_only"
    if "controlled_fusion" in sequence:
        return "bounded_wrapper"
    if "external_intake" in sequence:
        return "toolbox_catalog"
    if "governed_upgrade" in sequence:
        return "bounded_wrapper"
    return "reference_only"


def recommend_pipeline(*, change_summary: str, current_tier: str = "") -> dict[str, Any]:
    payload = build_evolution_pipeline()
    pipelines = payload.get("pipelines", []) if isinstance(payload.get("pipelines"), list) else []
    by_id = {
        str(item.get("pipeline_id", "")).strip(): item
        for item in pipelines
        if isinstance(item, dict) and str(item.get("pipeline_id", "")).strip()
    }
    text = _normalized_text(change_summary, current_tier)
    sequence = _recommend_sequence(text)
    target_tier = _recommend_target_tier(text, sequence)

    resolved_sequence: list[dict[str, Any]] = []
    for pipeline_id in sequence:
        item = by_id.get(pipeline_id)
        if not item:
            continue
        resolved_sequence.append(
            {
                "pipeline_id": pipeline_id,
                "display_name": str(item.get("display_name", "")).strip(),
                "purpose": str(item.get("purpose", "")).strip(),
                "default_target_tier": str(item.get("default_target_tier", "")).strip(),
                "touchpoints": item.get("touchpoints", []) if isinstance(item.get("touchpoints"), list) else [],
            }
        )

    reasons: list[str] = []
    if "external_intake" in sequence:
        reasons.append("The change looks like a newly introduced external capability, so AGN should assess fit before changing core behavior.")
    if "governed_upgrade" in sequence:
        reasons.append("The change mentions versioning or refresh concerns, so contracts and docs should be diffed before widening behavior.")
    if "controlled_fusion" in sequence:
        reasons.append("The change asks for integration or promotion, so the capability should enter through bounded wrappers and task-start support.")
    if "retirement_and_archive" in sequence:
        reasons.append("The change looks like cleanup or deprecation, so the safest path is a paused shim or archive flow.")

    next_touchpoints: list[dict[str, Any]] = []
    seen_touchpoints: set[str] = set()
    for item in resolved_sequence:
        for touchpoint in item.get("touchpoints", []):
            if not isinstance(touchpoint, dict):
                continue
            module_id = str(touchpoint.get("module_id", "")).strip()
            if not module_id or module_id in seen_touchpoints:
                continue
            seen_touchpoints.add(module_id)
            next_touchpoints.append(touchpoint)

    return {
        "schema_version": "agn.evolution_pipeline_recommendation.v1",
        "generated_at": utc_now_iso(),
        "package_path": PACKAGE_PATH,
        "legacy_script_shim": LEGACY_SCRIPT_SHIM,
        "change_summary": str(change_summary).strip(),
        "current_tier": str(current_tier).strip(),
        "recommended_sequence": resolved_sequence,
        "primary_pipeline_id": str(resolved_sequence[0]["pipeline_id"]) if resolved_sequence else "",
        "recommended_target_tier": target_tier,
        "reasons": reasons,
        "next_touchpoints": next_touchpoints,
        "operator_guidance": [
            "Do not jump directly to first_class_module unless repeated value and clean contracts are already proven.",
            "Keep governance and human authority outside the evolution pipeline; this module describes change flow, not policy ownership.",
            "If a repo only contributes patterns or prompts, stop at reference_only or toolbox_catalog.",
        ],
    }


def write_evolution_pipeline(payload: dict[str, Any], *, output_path: Path | None = None) -> Path:
    target = output_path or READ_MODEL_PATH
    atomic_write_json(target, payload)
    return target


def cmd_build(args: argparse.Namespace) -> int:
    payload = build_evolution_pipeline()
    if not args.no_write:
        target = Path(args.output).expanduser().resolve() if args.output else None
        write_evolution_pipeline(payload, output_path=target)
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def cmd_show(_args: argparse.Namespace) -> int:
    print(json.dumps(build_evolution_pipeline(), ensure_ascii=True, indent=2))
    return 0


def cmd_recommend(args: argparse.Namespace) -> int:
    print(
        json.dumps(
            recommend_pipeline(change_summary=str(args.change_summary).strip(), current_tier=str(args.current_tier or "").strip()),
            ensure_ascii=True,
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Describe how AGN ingests, fuses, upgrades, and retires capabilities without destabilizing the core.")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="Build and optionally write the evolution pipeline read model.")
    build.add_argument("--output", default="")
    build.add_argument("--no-write", action="store_true")
    build.set_defaults(func=cmd_build)

    show = sub.add_parser("show", help="Print the current evolution pipeline model.")
    show.set_defaults(func=cmd_show)

    recommend = sub.add_parser("recommend", help="Recommend an evolution pipeline and target tier for a proposed change.")
    recommend.add_argument("--change-summary", required=True)
    recommend.add_argument("--current-tier", default="")
    recommend.set_defaults(func=cmd_recommend)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
