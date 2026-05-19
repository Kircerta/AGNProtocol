#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import shutil
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_SRC = ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
DEFAULT_CODEX_HOME = Path.home() / ".codex_agn"
CONTROL_PLANE_APP = ROOT / "agn2" / "control_plane" / "src-tauri" / "target" / "release" / "bundle" / "macos" / "AGN2.0 Control Plane.app"
CONVERSATION_MONITOR_APP = ROOT / "agn2" / "conversation_monitor" / "src-tauri" / "target" / "release" / "bundle" / "macos" / "AGN Conversation Monitor.app"

try:
    from agn.core.desktop_provider import get_desktop_control_bin
    GUI_AGENT_BIN = get_desktop_control_bin()
except ImportError:  # pragma: no cover
    GUI_AGENT_BIN = Path.home() / ".codex" / "bin" / "gui-agent"
CONTROL_PLANE_INSTALLED_APP = Path("/Applications/AGN2.0 Control Plane.app")
CONVERSATION_MONITOR_INSTALLED_APP = Path("/Applications/AGN Conversation Monitor.app")

try:
    from provider_registry import probe_capabilities
except ImportError:  # pragma: no cover
    from scripts.provider_registry import probe_capabilities

try:
    from agn_external_toolbox import build_inventory as build_external_toolbox_inventory
except ImportError:  # pragma: no cover
    from scripts.agn_external_toolbox import build_inventory as build_external_toolbox_inventory

try:
    from agn_cognitive_overlays import list_overlays as list_cognitive_overlays
except ImportError:  # pragma: no cover
    from scripts.agn_cognitive_overlays import list_overlays as list_cognitive_overlays

try:
    from agn_tool_reality_cards import build_tool_reality_summary
except ImportError:  # pragma: no cover
    from scripts.agn_tool_reality_cards import build_tool_reality_summary


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _codex_home() -> Path:
    configured = str(os.getenv("CODEX_HOME", "")).strip()
    return Path(configured).expanduser() if configured else DEFAULT_CODEX_HOME


def _bool_path(path: Path) -> dict[str, Any]:
    return {"path": str(path), "available": path.exists()}


def _preferred_existing_path(*candidates: Path) -> Path:
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _app_surface_payload(name: str, *, build_path: Path, installed_path: Path, why: str, category: str) -> dict[str, Any]:
    preferred = _preferred_existing_path(installed_path, build_path)
    return {
        "available": build_path.exists() or installed_path.exists(),
        "entry": f"open '{preferred}'",
        "why": why,
        "category": category,
        "preferred_path": str(preferred),
        "build_artifact_path": str(build_path),
        "installed_app_path": str(installed_path),
        "name": name,
    }


def _skills_inventory() -> dict[str, Any]:
    codex_home = _codex_home()
    skills_root = codex_home / "skills"
    installed: list[str] = []
    agn_specific: list[str] = []
    system_skills: list[str] = []
    if skills_root.exists():
        for entry in sorted(skills_root.iterdir()):
            if entry.name == "TOOLBOX.md":
                continue
            if not entry.is_dir():
                continue
            if not (entry / "SKILL.md").exists():
                continue
            installed.append(entry.name)
            if entry.name.startswith("agn-") or entry.name == "gh-fix-ci":
                agn_specific.append(entry.name)
    system_root = codex_home / "skills" / ".system"
    if system_root.exists():
        for entry in sorted(system_root.iterdir()):
            if entry.is_dir() and (entry / "SKILL.md").exists():
                system_skills.append(entry.name)
    return {
        "codex_home": str(codex_home),
        "skills_root": str(skills_root),
        "installed_count": len(installed) + len(system_skills),
        "installed": installed,
        "agn_specific": agn_specific,
        "system_skills": system_skills,
    }


def _provider_summary(capabilities: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for lane in ("executors", "reviewers"):
        providers = capabilities.get(lane, {})
        available = [name for name, payload in sorted(providers.items()) if isinstance(payload, dict) and bool(payload.get("available"))]
        unavailable = [name for name, payload in sorted(providers.items()) if isinstance(payload, dict) and not bool(payload.get("available"))]
        summary[lane] = {
            "available": available,
            "unavailable": unavailable,
            "default": str(capabilities.get(f"default_{lane[:-1]}", "")).strip(),
        }
    return summary


def _provider_policy(capabilities: dict[str, Any]) -> dict[str, Any]:
    reviewers = capabilities.get("reviewers", {}) if isinstance(capabilities.get("reviewers"), dict) else {}
    executors = capabilities.get("executors", {}) if isinstance(capabilities.get("executors"), dict) else {}

    def _available(pool: dict[str, Any], name: str) -> bool:
        return bool((pool.get(name) or {}).get("available"))

    provider_roles = {
        "codex": {
            "grade": "central_execution",
            "allowed_for": ["planning", "integration", "verification", "high_trust_execution"],
            "forbidden_for": [],
            "notes": "Primary planner, integrator, verifier, and trusted executor.",
            "available": _available(executors, "codex") or _available(reviewers, "codex"),
        },
        "claude": {
            "grade": "flagship_reviewer",
            "allowed_for": ["hard_reasoning", "high_risk_review", "ambiguity_resolution", "architecture_review"],
            "forbidden_for": ["low_value_bulk_labor"],
            "notes": "Preferred flagship reviewer lane when materially useful.",
            "available": _available(reviewers, "claude") or _available(executors, "claude"),
        },
        "gemini": {
            "grade": "flagship_reviewer",
            "allowed_for": ["hard_reasoning", "high_risk_review", "ambiguity_resolution", "broad_read_only_audit"],
            "forbidden_for": ["low_value_bulk_labor"],
            "notes": "Fallback flagship reviewer lane after Claude for serious review.",
            "available": _available(reviewers, "gemini") or _available(executors, "gemini"),
        },
        "deepseek": {
            "grade": "worker_grade",
            "allowed_for": ["bounded_execution", "structured_transform", "general_analysis"],
            "forbidden_for": ["final_review", "governance_judgment", "destructive_decision", "final_authority"],
            "notes": "Cheap worker lane; not valid as a flagship reviewer.",
            "available": _available(reviewers, "deepseek") or _available(executors, "deepseek"),
        },
        "qwen_local": {
            "grade": "worker_grade",
            "allowed_for": ["bounded_execution", "structured_transform", "cleanup", "low_risk_repetition"],
            "forbidden_for": ["final_review", "governance_judgment", "destructive_decision", "final_authority"],
            "notes": "Local worker lane for tight, low-risk, bounded tasks only.",
            "available": _available(reviewers, "qwen_local") or _available(executors, "qwen_local"),
        },
    }
    return {
        "reviewer_policy": {
            "preferred_order": ["claude", "gemini"],
            "allowed_flagship_reviewers": ["claude", "gemini"],
            "worker_grade_models": ["qwen_local", "deepseek"],
            "forbidden_for_review": ["qwen_local", "deepseek"],
            "human_authority_required": True,
        },
        "provider_roles": provider_roles,
    }


def build_capability_snapshot() -> dict[str, Any]:
    provider_caps = probe_capabilities()
    skills = _skills_inventory()
    toolbox = build_external_toolbox_inventory()
    overlays = list_cognitive_overlays()
    tool_reality = build_tool_reality_summary()
    ghostty_path = shutil.which("ghostty") or ""
    cargo_path = shutil.which("cargo") or ""
    cargo_tauri_path = shutil.which("cargo-tauri") or ""
    tesseract_path = shutil.which("tesseract") or ""
    sips_path = shutil.which("sips") or ""
    python3_path = shutil.which("python3") or shutil.which("python") or ""

    surfaces = {
        "lifecycle": {
            "available": True,
            "entry": "python3 scripts/agn2_system.py status",
            "why": "Canonical lifecycle truth, mode, validation, and refresh surface.",
            "category": "authority_state",
        },
        "dispatcher": {
            "available": (ROOT / "scripts" / "dispatcher_runtime.py").exists(),
            "entry": "python3 scripts/dispatcher_runtime.py dispatch --from-json-file <request.json>",
            "why": "Single dispatch entry for providers, reviewer, memory, vision, desktop, and compatibility tasks.",
            "category": "execution",
        },
        "worker_delegate": {
            "available": (ROOT / "scripts" / "agn2_execution_workflow.py").exists(),
            "entry": "python3 scripts/agn2_execution_workflow.py delegate --instruction \"...\"",
            "why": "Bounded worker-grade delegation with routing and inspectable output.",
            "category": "execution",
        },
        "flagship_review": {
            "available": (ROOT / "scripts" / "review_orchestrator.py").exists(),
            "entry": "python3 scripts/agn2_execution_workflow.py review --file <path> --goal \"...\"",
            "why": "Structured external review for ambiguity, architecture, and high-risk verification.",
            "category": "review",
        },
        "desktop_control": {
            "available": GUI_AGENT_BIN.exists(),
            "entry": "python3 scripts/desktop_adapter.py",
            "why": "Governed GUI and Ghostty observation or action path with audit-linked execution gates.",
            "category": "execution",
        },
        "vision_parser": {
            "available": (ROOT / "scripts" / "vision_parser.py").exists() and bool(sips_path) and bool(tesseract_path),
            "entry": "python3 scripts/agn_governed_execution.py vision --task-id <id> --image-ref agn://artifact/<sha256>",
            "why": "Governed visual extraction into summary, OCR, entities, regions, and UI tree artifacts through the dispatcher.",
            "category": "observation",
        },
        "message_bus": {
            "available": (ROOT / "scripts" / "runtime_bus.py").exists(),
            "entry": "python3 scripts/runtime_bus.py",
            "why": "Append-only inter-agent transport with topic filtering, ack, TTL, and dead-letter handling.",
            "category": "runtime",
        },
        "memory_recorder": {
            "available": (ROOT / "scripts" / "memory_recorder.py").exists(),
            "entry": "python3 scripts/dispatcher_runtime.py dispatch --from-json-file <memory-request.json>",
            "why": "Append-only fact, decision, todo, and constraint recording without compaction drift.",
            "category": "memory",
        },
        "control_plane": _app_surface_payload(
            "AGN2.0 Control Plane",
            build_path=CONTROL_PLANE_APP,
            installed_path=CONTROL_PLANE_INSTALLED_APP,
            why="Formal human control surface reading canonical read models and writing formal commands.",
            category="authority_control",
        ),
        "conversation_monitor": _app_surface_payload(
            "AGN Conversation Monitor",
            build_path=CONVERSATION_MONITOR_APP,
            installed_path=CONVERSATION_MONITOR_INSTALLED_APP,
            why="Language-layer observation surface when evidence is conversational or trace-like.",
            category="observation",
        ),
        "external_toolbox": {
            "available": bool(toolbox.get("count", 0)),
            "entry": "python3 scripts/agn_external_toolbox.py list",
            "why": "Curated execution-layer mounts for browser automation, memory, evals, workflow discipline, and backend capability references.",
            "category": "execution_support",
        },
        "host_info": {
            "available": (ROOT / "scripts" / "agn_host_info.py").exists(),
            "entry": "python3 scripts/agn_host_info.py show",
            "why": "Single-host hardware, dependency, and local capability surface for the active AGN environment.",
            "category": "runtime",
        },
        "infrastructure_map": {
            "available": (ROOT / "scripts" / "agn_infrastructure_map.py").exists(),
            "entry": "python3 scripts/agn_infrastructure_map.py show",
            "why": "Agent-facing city map of AGN districts, active modules, entry points, and paused compatibility surfaces.",
            "category": "execution_support",
        },
        "operator_brief": {
            "available": (ROOT / "scripts" / "agn_operator_brief.py").exists(),
            "entry": "python3 scripts/agn_operator_brief.py build --task-summary \"...\"",
            "why": "Low-noise task-start brief that separates blocking issues, attention items, and informational context.",
            "category": "execution_support",
        },
        "task_start_kernel": {
            "available": (ROOT / "scripts" / "agn_task_start_kernel.py").exists(),
            "entry": "python3 scripts/agn_task_start_kernel.py build --task-summary \"...\"",
            "why": "Unified task-start kernel for local host facts, advisory memory priors, and relevant tool reality cards.",
            "category": "execution_support",
        },
        "cognitive_overlays": {
            "available": bool(overlays),
            "entry": "python3 scripts/agn_cognitive_overlays.py recommend --task-summary \"...\"",
            "why": "AGN-native reasoning overlays for coding critique, agent evals, academic writing, and memory-aware startup.",
            "category": "execution_support",
        },
        "tool_reality_cards": {
            "available": bool(tool_reality.get("count", 0)),
            "entry": "python3 scripts/agn_tool_reality_cards.py build",
            "why": "Host-aware capability cards explaining what key tools can and cannot do right now, including prerequisites and session limits.",
            "category": "execution_support",
        },
        "evolution_pipeline": {
            "available": (ROOT / "scripts" / "agn_evolution_pipeline.py").exists(),
            "entry": "python3 scripts/agn_evolution_pipeline.py show",
            "why": "Architecture surface for capability intake, bounded fusion, governed upgrades, and cleanup without destabilizing AGN core.",
            "category": "execution_support",
        },
        "reconstruction_status": {
            "available": (ROOT / "scripts" / "agn_reconstruction_status.py").exists(),
            "entry": "python3 scripts/agn_reconstruction_status.py show",
            "why": "Canonical restructuring tracker for AGN phases, milestones, and architectural handoff continuity.",
            "category": "execution_support",
        },
        "governed_execution_gateway": {
            "available": (ROOT / "scripts" / "agn_governed_execution.py").exists(),
            "entry": "python3 scripts/agn_governed_execution.py",
            "why": "Typed facade for provider, review, memory, vision, and desktop execution through the dispatcher.",
            "category": "execution_support",
        },
    }

    modules = {
        "dispatcher": {
            "target_kinds": ["provider", "reviewer", "memory_recorder", "vision_parser", "desktop_adapter", "legacy_task"],
            "required_request_fields": ["trace_id", "caller", "target", "target_kind", "intent", "reason"],
            "output_fields": ["request_id", "trace_id", "task_id", "target_kind", "result_ref", "failure_class"],
        },
        "reviewer": {
            "providers": _provider_summary(provider_caps).get("reviewers", {}),
            "round_policy": {"default_max_rounds": 1, "hard_cap": 2},
            "structured_schema": ["verdict", "confidence", "core_reasoning", "risks", "missing_evidence", "recommended_action", "escalate_to_human"],
            "abort_policy": [
                "abort reviewer calls when review is forbidden or local verification settles the question",
                "abort automatic acceptance when reviewer evidence is missing or provider execution fails",
                "escalate unresolved ambiguity to the operator after the bounded review rounds",
            ],
        },
        "memory_recorder": {
            "append_only": True,
            "categories": ["fact", "decision", "todo", "constraint", "incident", "evidence", "status"],
            "storage_root": "memory/records",
            "retention_policy": {
                "delete_in_place": False,
                "default_posture": "hot_append_only",
                "archival_mode": "human_approved_or_governed_retention_sweep",
                "conflict_handling": "append_new_record_or_preserve_conflict_artifact_never_silent_overwrite",
                "invalid_append_policy": "quarantine_invalid_payload_and_preserve_rejection_evidence",
            },
        },
        "vision_parser": {
            "accepts": ["agn://artifact/<sha256>", "local file path through --image-path registration helper"],
            "produces": ["summary.txt", "entities.json", "regions.json", "ocr.txt", "ocr.json", "ui_tree.json", "security.json", "optional *.evidence.* artifacts when redaction is triggered"],
            "requires_base64": False,
            "security_boundary": [
                "default_to_summary_and_structured_outputs_not_raw_image_blobs",
                "treat_visual_hits_as_evidence_not_authority_for_privileged_actions",
                "request_fresh_capture_or_human_review_when_visual_evidence_is_ambiguous",
                "quarantine_sensitive_auth_or_secret_surfaces_before_gui_execution",
                "emit_redacted_ocr_outputs_plus_security_scan_artifacts_when_sensitive_hits_are_detected",
                "preserve_raw_visual_evidence_in_additive_artifacts_for_audited_human_review_not_default_consumption",
            ],
        },
        "message_bus": {
            "storage_root": "runtime/bus",
            "features": ["append_only", "topic_filtering", "ack", "ttl", "dead_letter", "traceable_routes"],
        },
        "desktop_adapter": {
            "observe_surfaces": ["status", "frontmost", "mouse_position", "screenshot", "ghostty_status", "ghostty_windows", "ghostty_tabs", "ghostty_terminals"],
            "write_actions": ["TERMINAL_SPAWN", "TERMINAL_INPUT", "TERMINAL_SEND_KEY"],
            "write_requirements": ["allow_execute=true", "non-empty audit_refs", "approved policy gate"],
            "security_boundary": [
                "observe_first_and_plan_only_by_default",
                "no_write_actions_without_allow_execute_audit_refs_and_approved_gate",
                "missing_permissions_or_missing_gui_agent_should_abort_the_action_not_guess",
            ],
        },
    }

    toolchain = {
        "python3": {"path": python3_path, "available": bool(python3_path)},
        "ghostty": {"path": ghostty_path, "available": bool(ghostty_path)},
        "gui_agent": _bool_path(GUI_AGENT_BIN),
        "tesseract": {"path": tesseract_path, "available": bool(tesseract_path)},
        "sips": {"path": sips_path, "available": bool(sips_path)},
        "cargo": {"path": cargo_path, "available": bool(cargo_path)},
        "cargo_tauri": {"path": cargo_tauri_path, "available": bool(cargo_tauri_path)},
        "control_plane_app": {
            "path": str(_preferred_existing_path(CONTROL_PLANE_INSTALLED_APP, CONTROL_PLANE_APP)),
            "available": CONTROL_PLANE_INSTALLED_APP.exists() or CONTROL_PLANE_APP.exists(),
            "build_artifact_path": str(CONTROL_PLANE_APP),
            "installed_app_path": str(CONTROL_PLANE_INSTALLED_APP),
        },
        "conversation_monitor_app": {
            "path": str(_preferred_existing_path(CONVERSATION_MONITOR_INSTALLED_APP, CONVERSATION_MONITOR_APP)),
            "available": CONVERSATION_MONITOR_INSTALLED_APP.exists() or CONVERSATION_MONITOR_APP.exists(),
            "build_artifact_path": str(CONVERSATION_MONITOR_APP),
            "installed_app_path": str(CONVERSATION_MONITOR_INSTALLED_APP),
        },
    }

    surface_taxonomy = {
        "authority_control": ["control_plane"],
        "authority_state": ["lifecycle"],
        "observation": ["conversation_monitor", "vision_parser"],
        "execution": ["dispatcher", "worker_delegate", "desktop_control"],
        "execution_support": ["external_toolbox", "infrastructure_map", "task_start_kernel", "operator_brief", "cognitive_overlays", "tool_reality_cards", "evolution_pipeline", "reconstruction_status", "governed_execution_gateway"],
        "review": ["flagship_review"],
        "memory": ["memory_recorder"],
        "runtime": ["message_bus", "host_info"],
    }
    provider_policy = _provider_policy(provider_caps)

    guidance = [
        "Run agn2_system status or preflight before substantial work.",
        "Use desktop and vision surfaces when GUI state is better seen than inferred.",
        "Delegate bounded low-risk work; keep final judgment in Codex.",
        "Use flagship review only for ambiguity, high-risk change, or external audit.",
        "Record memory append-only; do not compact or overwrite core memory files.",
    ]

    return {
        "generated_at": utc_now_iso(),
        "repo_root": str(ROOT),
        "codex_home": skills["codex_home"],
        "skills": skills,
        "toolbox": toolbox,
        "tool_reality_cards": tool_reality,
        "cognitive_overlays": overlays,
        "toolchain": toolchain,
        "provider_capabilities": provider_caps,
        "provider_policy": provider_policy,
        "surfaces": surfaces,
        "surface_taxonomy": surface_taxonomy,
        "modules": modules,
        "guidance": guidance,
    }


def main() -> int:
    print(json.dumps(build_capability_snapshot(), ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
