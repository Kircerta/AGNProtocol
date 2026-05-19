"""AGN reconstruction status surface.

This is the real package implementation for AGN's reconstruction tracker and
read-model helper. The legacy script remains as a compatibility shim.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
from agn.core.admin_control import atomic_write_json, read_models_dir


TRACKER_PATH = ROOT / "config" / "reconstruction_tracker.json"
READ_MODEL_PATH = read_models_dir() / "reconstruction_status.json"
PACKAGE_PATH = "agn.governance.reconstruction_status"
LEGACY_SCRIPT_SHIM = "scripts/agn_reconstruction_status.py"


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _tracker() -> dict[str, Any]:
    payload = _load_json(TRACKER_PATH, {})
    return payload if isinstance(payload, dict) else {}


def build_reconstruction_status() -> dict[str, Any]:
    tracker = _tracker()
    program = tracker.get("program", {}) if isinstance(tracker.get("program"), dict) else {}
    phases = tracker.get("phases", []) if isinstance(tracker.get("phases"), list) else []
    milestones = tracker.get("milestone_log", []) if isinstance(tracker.get("milestone_log"), list) else []
    component_classes = tracker.get("component_classes", []) if isinstance(tracker.get("component_classes"), list) else []
    caveats = tracker.get("host_caveats", []) if isinstance(tracker.get("host_caveats"), list) else []
    active_phase_id = str(program.get("active_phase_id", "")).strip()

    phase_rows: list[dict[str, Any]] = []
    for item in phases:
        if not isinstance(item, dict):
            continue
        phase_rows.append(
            {
                "phase_id": str(item.get("phase_id", "")).strip(),
                "display_name": str(item.get("display_name", "")).strip(),
                "status": str(item.get("status", "")).strip() or "planned",
                "summary": str(item.get("summary", "")).strip(),
                "completion_commit": str(item.get("completion_commit", "")).strip(),
            }
        )

    current_phase = {}
    if active_phase_id:
        current_phase = next((item for item in phase_rows if str(item.get("phase_id", "")).strip() == active_phase_id), {})
    if not current_phase:
        current_phase = next((item for item in phase_rows if str(item.get("status", "")).strip() in {"next", "active"}), {})

    return {
        "schema_version": "agn.reconstruction_status.v1",
        "generated_at": utc_now_iso(),
        "package_path": PACKAGE_PATH,
        "legacy_script_shim": LEGACY_SCRIPT_SHIM,
        "program": program,
        "current_phase": current_phase,
        "phase_counts": {
            "completed": sum(1 for item in phase_rows if str(item.get("status", "")).strip() == "completed"),
            "active_or_next": sum(1 for item in phase_rows if str(item.get("status", "")).strip() in {"active", "next"}),
            "planned": sum(1 for item in phase_rows if str(item.get("status", "")).strip() == "planned"),
        },
        "phases": phase_rows,
        "latest_milestone": milestones[-1] if milestones else {},
        "milestone_count": len(milestones),
        "component_classes": component_classes,
        "host_caveats": caveats,
        "operator_guidance": [
            "Use this surface as the canonical handoff state for AGN restructuring work.",
            "Check current_phase before proposing more architectural churn.",
            "Append new milestones instead of rewriting older entries when progress advances.",
        ],
    }


def write_reconstruction_status(payload: dict[str, Any], *, output_path: Path | None = None) -> Path:
    target = output_path or READ_MODEL_PATH
    atomic_write_json(target, payload)
    return target


def recommend_next_step(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    status = payload if isinstance(payload, dict) else build_reconstruction_status()
    current_phase = status.get("current_phase", {}) if isinstance(status.get("current_phase"), dict) else {}
    phase_id = str(current_phase.get("phase_id", "")).strip()
    summary = str(current_phase.get("summary", "")).strip()
    if phase_id == "phase_2_governance_enforcement_boundary":
        next_step = "Narrow direct side-effect execution behind dispatcher-owned boundaries before deeper file migration."
    elif phase_id == "phase_3_gradual_implementation_migration":
        next_step = "Migrate one low-dependency module from scripts/ into src/agn and leave a shim."
    elif phase_id == "phase_4_persistent_control_loop":
        next_step = "Define host-aware daemon startup expectations without assuming every machine has Tauri/Rust dependencies."
    elif phase_id == "phase_5_end_to_end_governance_exercise":
        next_step = "Run one governed end-to-end exercise and leave inspectable artifacts."
    else:
        next_step = "Refresh the tracker before starting architectural work."
    return {
        "schema_version": "agn.reconstruction_recommendation.v1",
        "generated_at": utc_now_iso(),
        "phase_id": phase_id,
        "phase_summary": summary,
        "next_step": next_step,
    }


def cmd_build(args: argparse.Namespace) -> int:
    payload = build_reconstruction_status()
    if not args.no_write:
        target = Path(args.output).expanduser().resolve() if args.output else None
        write_reconstruction_status(payload, output_path=target)
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def cmd_show(_args: argparse.Namespace) -> int:
    print(json.dumps(build_reconstruction_status(), ensure_ascii=True, indent=2))
    return 0


def cmd_next(_args: argparse.Namespace) -> int:
    print(json.dumps(recommend_next_step(), ensure_ascii=True, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Track AGN reconstruction phases, milestones, and architectural handoff state.")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="Build and optionally write the reconstruction status read model.")
    build.add_argument("--output", default="")
    build.add_argument("--no-write", action="store_true")
    build.set_defaults(func=cmd_build)

    show = sub.add_parser("show", help="Print the current reconstruction status.")
    show.set_defaults(func=cmd_show)

    next_cmd = sub.add_parser("next", help="Recommend the next reconstruction step from the current tracker.")
    next_cmd.set_defaults(func=cmd_next)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))
