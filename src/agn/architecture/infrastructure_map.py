"""AGN city map surface.

This is the real package implementation for AGN's infrastructure map: the
district and module directory that helps agents navigate the system without
rediscovering raw files. The legacy script remains as a compatibility shim.
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
from capability_snapshot import build_capability_snapshot


PACKAGE_PATH = "agn.architecture.infrastructure_map"
LEGACY_SCRIPT_SHIM = "scripts/agn_infrastructure_map.py"
REGISTRY_PATH = ROOT / "config" / "infrastructure_registry.json"
READ_MODEL_PATH = read_models_dir() / "infrastructure_map.json"


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


def _module_availability(module: dict[str, Any], surfaces: dict[str, Any]) -> tuple[bool, str, str]:
    surface_ref = str(module.get("surface_ref", "")).strip()
    if surface_ref and isinstance(surfaces.get(surface_ref), dict):
        payload = surfaces[surface_ref]
        return bool(payload.get("available")), str(payload.get("entry", "")).strip(), "surface"

    script_path = str(module.get("script_path", "")).strip()
    if script_path:
        exists = (ROOT / script_path).exists()
        return exists, str(module.get("entry", "")).strip(), "script"
    return False, str(module.get("entry", "")).strip(), "registry"


def build_infrastructure_map() -> dict[str, Any]:
    registry = _registry()
    capability = build_capability_snapshot()
    surfaces = capability.get("surfaces", {}) if isinstance(capability.get("surfaces"), dict) else {}
    districts = registry.get("districts", []) if isinstance(registry.get("districts"), list) else []
    modules = registry.get("modules", []) if isinstance(registry.get("modules"), list) else []

    district_rollup: dict[str, dict[str, Any]] = {}
    for district in districts:
        if not isinstance(district, dict):
            continue
        district_id = str(district.get("district_id", "")).strip()
        if not district_id:
            continue
        district_rollup[district_id] = {
            "district_id": district_id,
            "display_name": str(district.get("display_name", "")).strip(),
            "purpose": str(district.get("purpose", "")).strip(),
            "module_count": 0,
            "active_count": 0,
            "compat_paused_count": 0,
        }

    resolved_modules: list[dict[str, Any]] = []
    for module in modules:
        if not isinstance(module, dict):
            continue
        available, resolved_entry, resolution_source = _module_availability(module, surfaces)
        status = str(module.get("status", "")).strip() or "active"
        district_id = str(module.get("district", "")).strip()
        if district_id in district_rollup:
            district_rollup[district_id]["module_count"] += 1
            if status == "active":
                district_rollup[district_id]["active_count"] += 1
            elif status == "compat_paused":
                district_rollup[district_id]["compat_paused_count"] += 1
        resolved_modules.append(
            {
                "module_id": str(module.get("module_id", "")).strip(),
                "display_name": str(module.get("display_name", "")).strip(),
                "district": district_id,
                "status": status,
                "purpose": str(module.get("purpose", "")).strip(),
                "entry": str(module.get("entry", "")).strip(),
                "resolved_entry": resolved_entry or str(module.get("entry", "")).strip(),
                "resolution_source": resolution_source,
                "available": available,
                "depends_on": list(module.get("depends_on", [])) if isinstance(module.get("depends_on", []), list) else [],
                "tags": list(module.get("tags", [])) if isinstance(module.get("tags", []), list) else [],
            }
        )

    return {
        "schema_version": "agn.infrastructure_map.v1",
        "generated_at": utc_now_iso(),
        "package_path": PACKAGE_PATH,
        "legacy_script_shim": LEGACY_SCRIPT_SHIM,
        "districts": [district_rollup[key] for key in sorted(district_rollup)],
        "modules": resolved_modules,
        "operator_guidance": [
            "Treat this map as AGN's city directory: active districts first, compatibility district last.",
            "Prefer modules in task_start before diving into execution or desktop work.",
            "Use compatibility-district modules only for explicit compatibility tasks.",
        ],
    }


def recommend_modules(*, task_summary: str, infrastructure_map: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = infrastructure_map if isinstance(infrastructure_map, dict) else build_infrastructure_map()
    modules = payload.get("modules", []) if isinstance(payload.get("modules"), list) else []
    by_id = {str(item.get("module_id", "")).strip(): item for item in modules if isinstance(item, dict)}

    chosen: list[tuple[str, str]] = [
        ("task_start_kernel", "Start from the unified task-start object instead of recomputing host, memory, and tool context by hand."),
        ("preflight", "Use the richer governed startup payload once the kernel is understood."),
        ("host_info", "Confirm the current machine's dependencies and paths before assuming anything."),
    ]
    text = str(task_summary or "").lower()
    if any(token in text for token in ("browser", "chrome", "twitter", "x.com", "web", "site", "page", "search")):
        chosen.extend(
            [
                ("browser_use_wrapper", "Browser and social tasks should default to the bounded browser wrapper."),
                ("tool_reality_cards", "Tool cards explain whether browser automation and desktop-adjacent tools fit the current host."),
            ]
        )
    if any(token in text for token in ("review", "risk", "architecture", "ambiguous", "correctness")):
        chosen.append(("flagship_review", "High-risk or ambiguous work should reserve the flagship review lane."))
    if any(token in text for token in ("dispatch", "governed", "gateway", "provider", "review", "memory", "vision", "desktop", "handler", "side-effect", "restructure", "restructuring", "reconstruction", "refactor", "architecture")):
        chosen.append(("governed_execution_gateway", "Active execution surfaces should prefer the governed gateway over direct handler calls."))
    if any(token in text for token in ("delegate", "batch", "normalize", "cleanup", "extract", "bounded")):
        chosen.append(("worker_delegate", "Bounded repetitive labor belongs on a worker path, not inside Codex judgment loops."))
    if any(token in text for token in ("memory", "recall", "context", "history")):
        chosen.append(("memory_recall", "Context-sensitive tasks should explicitly consult priors instead of trusting chat residue."))
    if any(token in text for token in ("github", "repo", "repository", "open source", "upgrade", "integrate", "fusion", "evolve", "evolution", "archive", "retire", "cleanup")):
        chosen.append(("evolution_pipeline", "Capability intake, upgrades, and cleanup should follow AGN's explicit evolution path instead of ad hoc core surgery."))
    if any(token in text for token in ("restructure", "restructuring", "reconstruction", "package", "migration", "proxy", "src/agn", "refactor")):
        chosen.append(("reconstruction_status", "Structural AGN changes should start from the reconstruction tracker so progress and boundaries stay continuous across agents."))
    if any(token in text for token in ("governance", "control plane", "lifecycle", "status", "policy")):
        chosen.extend(
            [
                ("agn2_system", "Governance and lifecycle questions should start from the canonical system surface."),
                ("control_plane", "Formal control and read-model visibility belong in the control plane."),
            ]
        )

    seen: set[str] = set()
    recommendations: list[dict[str, Any]] = []
    for module_id, reason in chosen:
        if module_id in seen:
            continue
        seen.add(module_id)
        module = by_id.get(module_id)
        if not module:
            continue
        recommendations.append(
            {
                "module_id": module_id,
                "display_name": str(module.get("display_name", "")).strip(),
                "district": str(module.get("district", "")).strip(),
                "resolved_entry": str(module.get("resolved_entry", "")).strip(),
                "reason": reason,
                "status": str(module.get("status", "")).strip(),
                "available": bool(module.get("available")),
            }
        )

    avoid = [
        {
            "module_id": str(module.get("module_id", "")).strip(),
            "display_name": str(module.get("display_name", "")).strip(),
            "reason": "Compatibility district only. Do not use by default.",
        }
        for module in modules
        if isinstance(module, dict) and str(module.get("status", "")).strip() == "compat_paused"
    ]

    return {
        "schema_version": "agn.infrastructure_recommendation.v1",
        "generated_at": utc_now_iso(),
        "package_path": PACKAGE_PATH,
        "legacy_script_shim": LEGACY_SCRIPT_SHIM,
        "task_summary": str(task_summary).strip(),
        "recommendations": recommendations,
        "avoid_by_default": avoid,
    }


def write_infrastructure_map(payload: dict[str, Any], *, output_path: Path | None = None) -> Path:
    target = output_path or READ_MODEL_PATH
    atomic_write_json(target, payload)
    return target


def cmd_build(args: argparse.Namespace) -> int:
    payload = build_infrastructure_map()
    if not args.no_write:
        write_infrastructure_map(payload, output_path=Path(args.output).expanduser().resolve() if args.output else None)
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def cmd_show(_args: argparse.Namespace) -> int:
    print(json.dumps(build_infrastructure_map(), ensure_ascii=True, indent=2))
    return 0


def cmd_recommend(args: argparse.Namespace) -> int:
    print(json.dumps(recommend_modules(task_summary=str(args.task_summary).strip()), ensure_ascii=True, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build AGN's infrastructure map: the district and module directory for agents and operators.")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="Build and optionally write the infrastructure map read model.")
    build.add_argument("--output", default="")
    build.add_argument("--no-write", action="store_true")
    build.set_defaults(func=cmd_build)

    show = sub.add_parser("show", help="Print the current infrastructure map.")
    show.set_defaults(func=cmd_show)

    recommend = sub.add_parser("recommend", help="Recommend modules for a task summary.")
    recommend.add_argument("--task-summary", required=True)
    recommend.set_defaults(func=cmd_recommend)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
