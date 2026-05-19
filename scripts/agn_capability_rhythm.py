#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "reports" / "capability_rhythm"

try:
    from agn.governance.execution_workflow import build_preflight_payload, system_snapshot
except ImportError:  # pragma: no cover - package import fallback
    from scripts.agn2_execution_workflow import build_preflight_payload, system_snapshot

try:
    from capability_snapshot import build_capability_snapshot
except ImportError:  # pragma: no cover - package import fallback
    from scripts.capability_snapshot import build_capability_snapshot

try:
    from agn_cognitive_overlays import recommend_overlays
except ImportError:  # pragma: no cover - package import fallback
    from scripts.agn_cognitive_overlays import recommend_overlays


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _safe_slug(text: str, *, default: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(text or "").strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or default


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def infer_needs(task_summary: str) -> dict[str, bool]:
    text = str(task_summary or "").lower()
    return {
        "needs_control_plane": any(token in text for token in ("control plane", "read model", "governance", "status", "lifecycle", "policy gate")),
        "needs_desktop": any(token in text for token in ("gui", "desktop", "window", "screen", "screenshot", "vision", "click", "mouse", "ghostty")),
        "needs_history": any(token in text for token in ("history", "conversation", "trace", "monitor", "audit trail")),
        "needs_worker": any(token in text for token in ("delegate", "summarize", "extract", "normalize", "cleanup", "repeat", "batch", "bounded")),
        "needs_review": any(token in text for token in ("architecture", "ambiguous", "risk", "review", "approval", "hard reasoning", "uncertain")),
    }


def recommend_skills(*, capability: dict[str, Any], needs: dict[str, bool], task_summary: str) -> list[dict[str, str]]:
    installed = set(capability.get("skills", {}).get("installed", []))
    allow_all = not installed
    text = str(task_summary or "").lower()
    candidates = [
        ("agn-system-entry", "Rebuild AGN2.0 context and choose the right execution surface before acting."),
        ("agn-capability-rhythm", "Synthesize status, capability snapshot, preflight, and the initial surface/provider decision."),
        ("agn-visual-operator", "Standardize screenshot, vision parsing, target finding, and GUI action planning."),
        ("agn-bounded-delegation", "Split bounded worker labor from Codex-owned judgment and emit a safe delegate payload."),
        ("agn-worker-review", "Keep worker routing and flagship review discipline explicit."),
        ("agn-desktop-evidence", "Preserve screenshots, desktop observations, and artifact-backed visual evidence."),
        ("agn-control-plane-ops", "Use the formal control plane and read-model operator path."),
        ("agn-memory-refresh", "Refresh long-term operating memory after skill or protocol drift."),
    ]
    selected: list[tuple[str, str]] = [candidates[0]]
    if needs.get("needs_desktop"):
        selected.extend([candidates[2], candidates[5]])
    if needs.get("needs_worker"):
        selected.extend([candidates[3], candidates[4]])
    if needs.get("needs_control_plane"):
        selected.append(candidates[6])
    if any(token in text for token in ("skill", "protocol", "memory", "context", "refresh", "operating memory")):
        selected.append(candidates[7])
    if len(selected) == 1:
        selected.append(candidates[1])
    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for name, reason in selected:
        if name in seen:
            continue
        if not allow_all and not name.startswith("agn-") and name not in installed:
            continue
        seen.add(name)
        deduped.append({"name": name, "reason": reason})
    return deduped


def provider_plan(*, capability: dict[str, Any], needs: dict[str, bool], risk_level: str) -> dict[str, Any]:
    policy = capability.get("provider_policy", {})
    provider_roles = policy.get("provider_roles", {}) if isinstance(policy.get("provider_roles"), dict) else {}
    reviewer_policy = policy.get("reviewer_policy", {}) if isinstance(policy.get("reviewer_policy"), dict) else {}

    workers = [
        name
        for name in reviewer_policy.get("worker_grade_models", [])
        if bool((provider_roles.get(name) or {}).get("available"))
    ]
    reviewers = [
        name
        for name in reviewer_policy.get("preferred_order", [])
        if bool((provider_roles.get(name) or {}).get("available"))
    ]
    worker = workers[0] if workers and needs.get("needs_worker") else ""
    reviewer = reviewers[0] if reviewers and (needs.get("needs_review") or risk_level in {"medium", "high"}) else ""

    return {
        "controller": {
            "provider": "codex",
            "role": "planner_integrator_verifier",
            "reason": "Codex remains the central execution unit and final integrator.",
        },
        "worker": {
            "provider": worker,
            "needed": bool(worker),
            "reason": "Use the cheapest valid worker-grade lane for bounded low-risk labor." if worker else "No worker lane is required for this task shape.",
        },
        "reviewer": {
            "provider": reviewer,
            "needed": bool(reviewer),
            "reason": "Use flagship review for ambiguity, high-risk change, or external audit." if reviewer else "No flagship review lane is required by current risk and task shape.",
        },
    }


def build_rhythm_payload(
    *,
    task_summary: str,
    risk_level: str,
    explicit_flags: dict[str, bool],
) -> dict[str, Any]:
    inferred = infer_needs(task_summary)
    needs = {key: bool(explicit_flags.get(key) or inferred.get(key)) for key in inferred}
    capability = build_capability_snapshot()
    snapshot = system_snapshot()
    task_slug = _safe_slug(task_summary, default="task")
    preflight = build_preflight_payload(
        task_summary=task_summary,
        risk_level=risk_level,
        task_id=f"agn-rhythm-{task_slug[:48]}",
        trace_id=f"trace-agn-rhythm-{task_slug[:48]}",
        subsystem="agn2",
        needs_control_plane=needs["needs_control_plane"],
        needs_desktop=needs["needs_desktop"],
        needs_history=needs["needs_history"],
        needs_worker=needs["needs_worker"],
        needs_review=needs["needs_review"],
        snapshot=snapshot,
    )
    selected_skills = recommend_skills(capability=capability, needs=needs, task_summary=task_summary)
    overlays = recommend_overlays(task_summary)
    providers = provider_plan(capability=capability, needs=needs, risk_level=risk_level)
    decision_lines = [
        "Start with lifecycle truth and capability recall before opening into ad hoc execution.",
        "Choose the richest valid execution surface first, not the most familiar one.",
    ]
    if needs["needs_desktop"]:
        decision_lines.append("This task has visible or GUI state, so use screenshot or desktop observation before inferring.")
    if needs["needs_worker"]:
        decision_lines.append("Bounded low-risk transforms should be delegated before doing them manually.")
    if needs["needs_review"] or risk_level in {"medium", "high"}:
        decision_lines.append("Reserve flagship review for correctness, ambiguity, or higher operational risk.")
    if overlays:
        decision_lines.append("Use the recommended cognitive overlays when they materially improve critique, structure, or validation discipline.")
    return {
        "ok": True,
        "generated_at": utc_now_iso(),
        "task_summary": task_summary,
        "risk_level": risk_level,
        "inferred_needs": inferred,
        "effective_needs": needs,
        "startup_commands": [
            "python3 scripts/agn2_system.py status",
            "python3 scripts/agn2_system.py capabilities",
            f"python3 scripts/agn2_execution_workflow.py preflight --task-summary {json.dumps(task_summary)} --risk-level {risk_level}",
        ],
        "selected_skills": selected_skills,
        "recommended_overlays": overlays,
        "provider_plan": providers,
        "surface_plan": preflight.get("recommended_surfaces", []),
        "execution_checks": preflight.get("execution_checks", []),
        "task_start_kernel": preflight.get("task_start_kernel", {}),
        "host_info": preflight.get("host_info", {}),
        "decision_summary": decision_lines,
        "capability_snapshot_ref": str(ROOT / "runtime" / "admin_control" / "read_models" / "capability_snapshot.json"),
        "execution_discipline_ref": str(ROOT / "runtime" / "admin_control" / "read_models" / "execution_discipline.json"),
    }


def _write_report(payload: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = _safe_slug(str(payload.get("task_summary", "")), default="task")
    path = REPORT_DIR / f"{timestamp}-{slug[:60]}.json"
    latest = REPORT_DIR / "latest.json"
    _atomic_write_json(path, payload)
    _atomic_write_json(latest, payload)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize AGN startup rhythm for a concrete task using status, capabilities, and preflight.")
    parser.add_argument("--task-summary", required=True)
    parser.add_argument("--risk-level", choices=["low", "medium", "high"], default="low")
    parser.add_argument("--needs-control-plane", action="store_true")
    parser.add_argument("--needs-desktop", action="store_true")
    parser.add_argument("--needs-history", action="store_true")
    parser.add_argument("--needs-worker", action="store_true")
    parser.add_argument("--needs-review", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()

    payload = build_rhythm_payload(
        task_summary=str(args.task_summary).strip(),
        risk_level=str(args.risk_level).strip().lower(),
        explicit_flags={
            "needs_control_plane": bool(args.needs_control_plane),
            "needs_desktop": bool(args.needs_desktop),
            "needs_history": bool(args.needs_history),
            "needs_worker": bool(args.needs_worker),
            "needs_review": bool(args.needs_review),
        },
    )
    if not args.no_write:
        payload["report_path"] = str(_write_report(payload))
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
