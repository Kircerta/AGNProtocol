"""AGN control-plane read-model builder.

This is the real package implementation for AGN's governance-facing read model
aggregation. The legacy script remains only as a CLI compatibility shim.
"""
from __future__ import annotations

import argparse
from collections import Counter
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

from agn.architecture.evolution_pipeline import build_evolution_pipeline, write_evolution_pipeline
from agn.architecture.infrastructure_map import build_infrastructure_map, write_infrastructure_map
from agn.governance.reconstruction_status import build_reconstruction_status, write_reconstruction_status
from agn.runtime.host_info import build_host_info, write_host_info
from agn_api.ssot_store import SSOTStore
from capability_snapshot import build_capability_snapshot
from agn_tool_reality_cards import resolve_tool_reality_cards

try:
    from agn.core.admin_control import (
        admin_audit_path,
        append_admin_audit,
        atomic_write_json,
        load_json,
        load_jsonl,
        read_models_dir,
        repo_root,
    )
    from agn.core.emergency_stop import load_system_mode
    from agn.core.policy_gate import effective_gate_state, list_gate_entries, pending_gate_entries
except ImportError:  # pragma: no cover
    from agn.core.admin_control import (
        admin_audit_path,
        append_admin_audit,
        atomic_write_json,
        load_json,
        load_jsonl,
        read_models_dir,
        repo_root,
    )
    from agn.core.emergency_stop import load_system_mode
    from agn.core.policy_gate import effective_gate_state, list_gate_entries, pending_gate_entries


PACKAGE_PATH = "agn.governance.read_models"
LEGACY_SCRIPT_SHIM = "scripts/control_plane_read_model.py"


def _root() -> Path:
    return repo_root()


def _checkpoint_dir() -> Path:
    return _root() / ".agn_workspace" / "event_driven" / "ssot" / "checkpoints"


def _events_dir() -> Path:
    return _root() / ".agn_workspace" / "event_driven" / "ssot" / "events"


def _bus_index_path() -> Path:
    return _root() / "runtime" / "bus" / "index.jsonl"


def _bus_dead_letter_dir() -> Path:
    return _root() / "runtime" / "bus" / "dead_letter"


def _desktop_log_dir() -> Path:
    return _root() / "runtime" / "desktop_actions"


def _provider_caps_path() -> Path:
    return _root() / "runtime" / "provider_capabilities.json"


def _memory_records_dir() -> Path:
    return _root() / "memory" / "records"


def _ssot_dir() -> Path:
    return _root() / "ssot"


def _preflight_dir() -> Path:
    return _root() / "runtime" / "admin_control" / "preflight"


def _dispatcher_requests_dir() -> Path:
    return _root() / "runtime" / "dispatcher" / "requests"


def _dispatcher_results_dir() -> Path:
    return _root() / "runtime" / "dispatcher" / "results"


def _read_json(path: Path) -> dict[str, Any]:
    return load_json(path)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return load_jsonl(path)


def _parse_iso(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _model_generated_at(system_mode: dict[str, Any] | None = None) -> str:
    payload = system_mode if isinstance(system_mode, dict) else load_system_mode()
    updated = str(payload.get("updated_at", "")).strip()
    return updated or datetime.now(tz=timezone.utc).isoformat()


def _load_tasks() -> list[dict[str, Any]]:
    store = SSOTStore(_ssot_dir())
    tasks = store.list_tasks()
    checkpoints: dict[str, dict[str, Any]] = {}
    for path in sorted(_checkpoint_dir().glob("*.json")):
        payload = _read_json(path)
        task_id = str(payload.get("task_id", "")).strip()
        if task_id:
            checkpoints[task_id] = payload
    rows: list[dict[str, Any]] = []
    for task in tasks:
        task_id = str(task.get("id", "")).strip()
        cp = checkpoints.get(task_id, {})
        rows.append(
            {
                "task_id": task_id,
                "trace_id": str(task.get("correlation_id", cp.get("trace_id", ""))).strip(),
                "request_summary": str(task.get("request_summary", task.get("request_text", ""))).strip()[:180],
                "status": str(task.get("status", "")).strip() or str(cp.get("state", "unknown")).strip(),
                "state": str(cp.get("state", "unknown")).strip(),
                "paused": bool(cp.get("paused", False)),
                "review_requested": bool(task.get("review_requested", False)),
                "risk_level": str(task.get("risk_level", "low")).strip(),
                "executor_provider": str(task.get("executor_provider", "")).strip(),
                "reviewer_provider": str(task.get("reviewer_provider", "")).strip(),
                "updated_at": str(cp.get("updated_at", task.get("updated_at", ""))).strip(),
                "admin_hold": bool(cp.get("awaiting_admin_response", False)),
                "priority": str(task.get("priority", "normal")).strip() or "normal",
            }
        )
    rows.sort(key=lambda item: (item["updated_at"], item["task_id"]), reverse=True)
    return rows


def build_overview_model() -> dict[str, Any]:
    tasks = _load_tasks()
    gates = list_gate_entries()
    pending_gates = pending_gate_entries()
    dead_letters = sorted(_bus_dead_letter_dir().glob("*.json"))
    state_counts = Counter(str(item.get("state", "unknown")).strip() for item in tasks)
    active = [item for item in tasks if item["state"] not in {"DELIVERED", "ABORTED"}]
    blocked = [item for item in tasks if item["paused"] or item["admin_hold"] or item["state"] == "NEED_ADMIN"]
    recent_events: list[dict[str, Any]] = []
    for path in sorted(_events_dir().glob("*.jsonl"))[-20:]:
        recent_events.extend(_read_jsonl(path)[-8:])
    # H4: Filter out stale events older than 48 hours to prevent old
    # INTEGRITY_ALERTs from polluting current system status.
    # Escalation-class events (NEED_ADMIN, DISPATCHER_REQUEST_GATED,
    # COUNCIL_REVIEW_REQUIRED) are exempt from the TTL because they
    # represent tasks waiting for human action and must persist until
    # the underlying task state changes.
    _STALE_EVENT_TTL_SECONDS = 48 * 3600
    _ESCALATION_EVENT_TYPES = {"NEED_ADMIN", "DISPATCHER_REQUEST_GATED", "COUNCIL_REVIEW_REQUIRED"}
    _now = datetime.now(tz=timezone.utc)
    def _event_is_fresh(item: dict[str, Any]) -> bool:
        if str(item.get("event_type", "")).strip() in _ESCALATION_EVENT_TYPES:
            return True  # escalation-class events are never aged out
        raw_ts = str(item.get("ts", "")).strip()
        if not raw_ts:
            return True  # keep events without timestamps (conservative)
        parsed = _parse_iso(raw_ts)
        if parsed is None:
            return True
        age = (_now - parsed.astimezone(timezone.utc)).total_seconds()
        return age < _STALE_EVENT_TTL_SECONDS

    recent_failures = [
        {
            "trace_id": str(item.get("trace_id", "")).strip(),
            "task_id": str(item.get("task_id", "")).strip(),
            "event_type": str(item.get("event_type", "")).strip(),
            "ts": str(item.get("ts", "")).strip(),
        }
        for item in recent_events
        if str(item.get("severity", "info")).strip() in {"error", "warn"}
        and _event_is_fresh(item)
    ][-8:]
    recent_escalations = [
        item
        for item in recent_failures
        if item["event_type"] in _ESCALATION_EVENT_TYPES
    ][-8:]
    agn1_events: list[dict[str, Any]] = []
    for item in _read_jsonl(admin_audit_path())[-500:]:
        event_kind = str(item.get("kind", "")).strip()
        if event_kind.startswith("agn1.") or str(item.get("subsystem", "")).strip() == "agn1":
            agn1_events.append(
                {
                    "ts": str(item.get("ts", "")).strip(),
                    "kind": event_kind,
                    "worker": str(item.get("worker", "")).strip(),
                    "task_id": str(item.get("task_id", "")).strip(),
                    "detail": {k: v for k, v in item.items() if k not in {"ts", "kind", "worker", "task_id", "subsystem"}},
                }
            )
    agn1_native_audit = _root() / "audit" / "events.jsonl"
    for item in _read_jsonl(agn1_native_audit)[-200:]:
        action = str(item.get("action", "")).strip()
        if action in {
            "dispatch_created",
            "dispatch_gated_by_policy",
            "executor_completed",
            "reviewer_completed",
            "stale_dispatch_recovered",
            "hallucination_lock_triggered",
        }:
            agn1_events.append(
                {
                    "ts": str(item.get("ts", "")).strip(),
                    "kind": f"agn1.{action}",
                    "worker": str(item.get("route", "")).strip().lstrip("/"),
                    "task_id": str(item.get("task_id", "")).strip(),
                    "detail": {"status": item.get("status"), "attempt": item.get("attempt")},
                }
            )
    agn1_events.sort(key=lambda item: str(item.get("ts", "")))
    agn1_recent = agn1_events[-30:]
    system_mode = load_system_mode()
    return {
        "generated_at": _model_generated_at(system_mode),
        "system_mode": system_mode,
        "counts": {
            "active_tasks": len(active),
            "queued_tasks": sum(
                1
                for item in tasks
                if item["state"] in {"CREATED", "PLANNED", "DISPATCHED_EXEC", "DISPATCHED_REVIEW"}
            ),
            "blocked_tasks": len(blocked),
            "dead_letters": len(dead_letters),
            "policy_gate_pending": len(pending_gates),
            "policy_gate_total": len(gates),
        },
        "state_counts": dict(state_counts),
        "recent_failures": recent_failures,
        "recent_escalations": recent_escalations,
        "agn1_subsystem": {
            "recent_events": agn1_recent,
            "event_count": len(agn1_events),
            "last_event_ts": str(agn1_recent[-1]["ts"]) if agn1_recent else "",
        },
    }


def build_task_board_model() -> dict[str, Any]:
    rows = _load_tasks()
    return {"generated_at": _model_generated_at(), "items": rows}


def build_approval_gate_model() -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for item in list_gate_entries():
        gate_id = str(item.get("gate_id", "")).strip()
        state = effective_gate_state(gate_id)
        items.append({**item, "effective_status": str(state.get("status", "pending")).strip()})
    items.sort(key=lambda row: str(row.get("created_at", "")), reverse=True)
    return {"generated_at": _model_generated_at(), "items": items}


def build_agent_heartbeat_model() -> dict[str, Any]:
    caps = _read_json(_provider_caps_path())
    bus_entries = _read_jsonl(_bus_index_path())
    last_seen: dict[str, str] = {}
    for entry in bus_entries:
        for actor_key in ("from", "to"):
            actor = str(entry.get(actor_key, "")).strip()
            if not actor:
                continue
            last_seen[actor] = str(entry.get("ts", "")).strip()
    return {
        "generated_at": _model_generated_at(),
        "provider_capabilities": caps,
        "actors": [{"actor": key, "last_seen": value} for key, value in sorted(last_seen.items())],
    }


def _desktop_raw_entries(limit: int) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(_desktop_log_dir().glob("*.jsonl"))[-12:]:
        for item in _read_jsonl(path)[-20:]:
            entries.append(
                {
                    "ts": str(item.get("ts", item.get("timestamp", ""))).strip(),
                    "kind": "desktop_action",
                    "source": path.name,
                    "trace_id": str(item.get("trace_id", "")).strip(),
                    "payload": item,
                }
            )
    return entries[-limit:]


def _dispatcher_raw_entries(limit: int) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(_dispatcher_requests_dir().glob("*.json"))[-limit:]:
        payload = _read_json(path)
        entries.append(
            {
                "ts": str(payload.get("created_at", "")).strip(),
                "kind": "dispatcher_request",
                "trace_id": str(payload.get("trace_id", "")).strip(),
                "payload": {
                    "request_id": str(payload.get("request_id", "")).strip(),
                    "task_id": str(payload.get("task_id", "")).strip(),
                    "caller": str(payload.get("caller", "")).strip(),
                    "target_kind": str(payload.get("target_kind", "")).strip(),
                    "target": str(payload.get("target", "")).strip(),
                    "intent": str(payload.get("intent", "")).strip(),
                    "risk_level": str(payload.get("risk_level", "")).strip(),
                },
            }
        )
    for path in sorted(_dispatcher_results_dir().glob("*.json"))[-limit:]:
        payload = _read_json(path)
        result_payload = payload.get("result", {})
        if not isinstance(result_payload, dict):
            result_payload = {}
        entries.append(
            {
                "ts": str(payload.get("completed_at", "")).strip(),
                "kind": "dispatcher_result",
                "trace_id": str(payload.get("trace_id", "")).strip(),
                "payload": {
                    "request_id": str(payload.get("request_id", "")).strip(),
                    "task_id": str(payload.get("task_id", "")).strip(),
                    "target_kind": str(payload.get("target_kind", "")).strip(),
                    "target": str(payload.get("target", "")).strip(),
                    "ok": bool(payload.get("ok", False)),
                    "failure_class": str(payload.get("failure_class", "")).strip(),
                    "handler": str(result_payload.get("handler", "")).strip(),
                    "quarantined_any": bool(result_payload.get("quarantined_any", False)),
                    "redacted_any": bool(result_payload.get("redacted_any", False)),
                    "security_refs": list(result_payload.get("security_refs", []))
                    if isinstance(result_payload.get("security_refs", []), list)
                    else [],
                    "evidence_refs_present": bool(result_payload.get("evidence_refs_present", False)),
                },
            }
        )
    entries.sort(key=lambda item: str(item.get("ts", "")))
    return entries[-limit:]


def build_raw_stream_model(*, limit: int = 250) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    entries.extend(_dispatcher_raw_entries(limit))
    for item in _read_jsonl(_bus_index_path())[-limit:]:
        entries.append(
            {
                "ts": str(item.get("ts", "")).strip(),
                "kind": "bus",
                "trace_id": str(item.get("related_trace", "")).strip(),
                "payload": item,
            }
        )
    for path in sorted(_events_dir().glob("*.jsonl"))[-20:]:
        for item in _read_jsonl(path)[-20:]:
            entries.append(
                {
                    "ts": str(item.get("ts", "")).strip(),
                    "kind": "event",
                    "trace_id": str(item.get("trace_id", "")).strip(),
                    "payload": item,
                }
            )
    for item in _read_jsonl(admin_audit_path())[-limit:]:
        entries.append(
            {
                "ts": str(item.get("ts", "")).strip(),
                "kind": "admin_audit",
                "trace_id": str(item.get("trace_id", "")).strip(),
                "payload": item,
            }
        )
    entries.extend(_desktop_raw_entries(limit))
    for path in sorted(_bus_dead_letter_dir().glob("*.json"))[-20:]:
        payload = _read_json(path)
        entries.append(
            {
                "ts": str(payload.get("ts", "")).strip(),
                "kind": "dead_letter",
                "trace_id": str((payload.get("message", {}) if isinstance(payload.get("message"), dict) else {}).get("related_trace", "")).strip(),
                "payload": payload,
            }
        )
    entries.sort(key=lambda item: str(item.get("ts", "")))
    return {"generated_at": _model_generated_at(), "items": entries[-limit:]}


def build_memory_summary_model() -> dict[str, Any]:
    memory_dir = _memory_records_dir()
    count = sum(1 for _ in memory_dir.rglob("*.jsonl")) if memory_dir.exists() else 0
    return {"generated_at": _model_generated_at(), "record_files": count}


def build_execution_discipline_model(capability_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    snapshot = capability_snapshot if isinstance(capability_snapshot, dict) else {}
    preflight = _read_json(_preflight_dir() / "latest.json")
    generated_at = _model_generated_at()
    if not preflight:
        return {
            "generated_at": generated_at,
            "has_preflight": False,
            "status": "missing_preflight",
            "current_task": {},
            "execution_checks": [],
            "task_start_kernel": {},
            "operator_brief": {},
            "recommended_surfaces": [],
            "regression_signals": [],
            "provider_policy": snapshot.get("provider_policy") if isinstance(snapshot.get("provider_policy"), dict) else {},
            "surface_taxonomy": snapshot.get("surface_taxonomy") if isinstance(snapshot.get("surface_taxonomy"), dict) else {},
            "next_step": "Run agn2_execution_workflow.py preflight before substantial work.",
        }

    preflight_generated = str(preflight.get("generated_at", "")).strip()
    preflight_dt = _parse_iso(preflight_generated)
    age_seconds: int | None = None
    if preflight_dt is not None:
        age_seconds = max(0, int((datetime.now(tz=timezone.utc) - preflight_dt.astimezone(timezone.utc)).total_seconds()))
    execution_checks = preflight.get("execution_checks", [])
    if not isinstance(execution_checks, list):
        execution_checks = []
    check_counts = Counter(str(item.get("status", "unknown")).strip() for item in execution_checks if isinstance(item, dict))
    blocking_checks = [item for item in execution_checks if isinstance(item, dict) and str(item.get("status", "")).strip() == "blocked"]
    attention_checks = [item for item in execution_checks if isinstance(item, dict) and str(item.get("status", "")).strip() == "attention"]
    if blocking_checks:
        status = "blocked"
    elif attention_checks:
        status = "attention"
    else:
        status = "ready"
    operator_brief = preflight.get("operator_brief", {}) if isinstance(preflight.get("operator_brief"), dict) else {}
    if operator_brief:
        status = str(operator_brief.get("status", status)).strip() or status
    return {
        "generated_at": generated_at,
        "has_preflight": True,
        "status": status,
        "preflight_generated_at": preflight_generated,
        "preflight_age_seconds": age_seconds,
        "current_task": {
            "summary": str(preflight.get("task_summary", "")).strip(),
            "task_id": str(preflight.get("task_id", "")).strip(),
            "trace_id": str(preflight.get("trace_id", "")).strip(),
            "risk_level": str(preflight.get("risk_level", "")).strip(),
            "subsystem": str(preflight.get("subsystem", "")).strip(),
        },
        "execution_checks": execution_checks,
        "task_start_kernel": preflight.get("task_start_kernel", {}) if isinstance(preflight.get("task_start_kernel"), dict) else {},
        "check_counts": dict(check_counts),
        "recommended_surfaces": preflight.get("recommended_surfaces", []) if isinstance(preflight.get("recommended_surfaces"), list) else [],
        "operator_brief": operator_brief,
        "regression_signals": preflight.get("regression_signals", []) if isinstance(preflight.get("regression_signals"), list) else [],
        "next_actions": preflight.get("next_actions", []) if isinstance(preflight.get("next_actions"), list) else [],
        "provider_policy": snapshot.get("provider_policy") if isinstance(snapshot.get("provider_policy"), dict) else {},
        "surface_taxonomy": snapshot.get("surface_taxonomy") if isinstance(snapshot.get("surface_taxonomy"), dict) else {},
        "worker_and_review_state": preflight.get("worker_and_review_state", {}) if isinstance(preflight.get("worker_and_review_state"), dict) else {},
    }


def refresh_read_models() -> dict[str, Any]:
    read_models_dir().mkdir(parents=True, exist_ok=True)
    overview = build_overview_model()
    task_board = build_task_board_model()
    approval_gate = build_approval_gate_model()
    raw_stream = build_raw_stream_model()
    agent_heartbeat = build_agent_heartbeat_model()
    memory_summary = build_memory_summary_model()
    capability_snapshot = build_capability_snapshot()
    execution_discipline = build_execution_discipline_model(capability_snapshot)
    host_info = build_host_info(refresh=False)
    write_host_info(host_info)
    infrastructure_map = build_infrastructure_map()
    write_infrastructure_map(infrastructure_map)
    evolution_pipeline = build_evolution_pipeline()
    write_evolution_pipeline(evolution_pipeline)
    reconstruction_status = build_reconstruction_status()
    write_reconstruction_status(reconstruction_status)
    tool_reality_cards = resolve_tool_reality_cards()
    outputs = {
        "overview": overview,
        "task_board": task_board,
        "approval_gate": approval_gate,
        "raw_stream": raw_stream,
        "agent_heartbeat": agent_heartbeat,
        "memory_summary": memory_summary,
        "capability_snapshot": capability_snapshot,
        "execution_discipline": execution_discipline,
        "host_info": host_info,
        "infrastructure_map": infrastructure_map,
        "evolution_pipeline": evolution_pipeline,
        "reconstruction_status": reconstruction_status,
        "tool_reality_cards": tool_reality_cards,
    }
    for name, payload in outputs.items():
        atomic_write_json(read_models_dir() / f"{name}.json", payload)
    append_admin_audit("read_models_refreshed", generated=list(outputs))
    return {"ok": True, "generated": sorted(outputs)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build AGN2.0 control-plane read models.")
    parser.add_argument("command", choices=["refresh"])
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "refresh":
        print(json.dumps(refresh_read_models(), ensure_ascii=True))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
