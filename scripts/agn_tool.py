#!/usr/bin/env python3
"""AGN Tool — unified CLI for agent access to AGN system state.

Read-only tools for any agent; write tools gated by role.
All output is JSON for machine consumption.

Usage:
  python3 scripts/agn_tool.py health
  python3 scripts/agn_tool.py tasks [--status pending] [--limit 20]
  python3 scripts/agn_tool.py task <task_id>
  python3 scripts/agn_tool.py awakening
  python3 scripts/agn_tool.py providers
  python3 scripts/agn_tool.py gate-status [<gate_id>]
  python3 scripts/agn_tool.py memory-search <query> [--scope all] [--limit 10]
  python3 scripts/agn_tool.py artifact <ref>
  python3 scripts/agn_tool.py dispatch --json <payload_json>
  python3 scripts/agn_tool.py record-memory --kind fact --summary "..." [--scope global]
  python3 scripts/agn_tool.py log-event --action "tool_invoked" [--detail "..."]
  python3 scripts/agn_tool.py whoami
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
if str(ROOT / "agn_api") not in sys.path:
    sys.path.insert(0, str(ROOT / "agn_api"))

try:
    from agn_governed_execution import dispatch_memory_record
except ImportError:  # pragma: no cover - package import fallback
    from scripts.agn_governed_execution import dispatch_memory_record


def _out(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, indent=2))


def _safe_json(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ── Read-Only Commands ──────────────────────────────────────────────────


def cmd_health(_args: argparse.Namespace) -> int:
    """System health: mode, emergency stop, load, disk, agent count."""
    system_mode = _safe_json(ROOT / "runtime" / "admin_control" / "system_mode.json")
    lifecycle = _safe_json(ROOT / "runtime" / "admin_control" / "lifecycle" / "agn2_system.json")

    import shutil
    health: dict[str, Any] = {}
    try:
        l1, l5, l15 = os.getloadavg()
        health["load_avg"] = {"1m": round(l1, 2), "5m": round(l5, 2), "15m": round(l15, 2)}
    except Exception:
        pass
    try:
        stat = shutil.disk_usage(str(ROOT))
        health["disk_free_gb"] = round(stat.free / (1024**3), 1)
        health["disk_used_pct"] = round((stat.used / stat.total) * 100, 1)
    except Exception:
        pass

    _out({
        "ok": True,
        "ts": _utc_now(),
        "system_mode": system_mode.get("mode", "unknown"),
        "emergency_stop_active": bool(system_mode.get("emergency_stop_active", False)),
        "dispatcher_accepts_new_work": bool(system_mode.get("dispatcher_accepts_new_work", False)),
        "lifecycle_status": lifecycle.get("status", "unknown"),
        "last_refresh_at": lifecycle.get("last_refresh_at", ""),
        "health": health,
    })
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    """List tasks from SSOT with optional status filter."""
    from ssot_store import SSOTStore
    from task_engine import derive_status

    store = SSOTStore(ROOT / "ssot")
    status_filter = (args.status or "").strip().lower()
    limit = max(1, min(200, args.limit))

    tasks = []
    for f in sorted((ROOT / "ssot").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        task = store.get_task(f.stem)
        if task is None:
            continue
        s = derive_status(task)
        if status_filter and s != status_filter:
            continue
        tasks.append({
            "id": task.get("id", f.stem),
            "status": s,
            "source": task.get("source", ""),
            "risk_level": task.get("risk_level", "low"),
            "created_at": task.get("created_at", ""),
            "updated_at": task.get("updated_at", ""),
            "request_summary": str(task.get("request_text", ""))[:200],
        })
        if len(tasks) >= limit:
            break

    _out({"ok": True, "count": len(tasks), "tasks": tasks})
    return 0


def cmd_task(args: argparse.Namespace) -> int:
    """Get full detail for a single task."""
    from ssot_store import SSOTStore
    from task_engine import derive_status

    store = SSOTStore(ROOT / "ssot")
    task = store.get_task(args.task_id)
    if task is None:
        _out({"ok": False, "error": f"task_not_found:{args.task_id}"})
        return 1

    task["status"] = derive_status(task)
    # Redact any potential secrets
    safe = {k: v for k, v in task.items() if not k.startswith("_secret")}
    _out({"ok": True, "task": safe})
    return 0


def cmd_awakening(_args: argparse.Namespace) -> int:
    """Read current awakening state snapshot."""
    state_path = ROOT / "agn2" / "awakening" / "current_state.json"
    state = _safe_json(state_path)
    if not state:
        _out({"ok": False, "error": "awakening_state_not_available"})
        return 1
    _out({"ok": True, **state})
    return 0


def cmd_providers(_args: argparse.Namespace) -> int:
    """Check provider availability and routing guidance."""
    from provider_registry import probe_capabilities
    capabilities = probe_capabilities()
    _out({"ok": True, "ts": _utc_now(), **capabilities})
    return 0


def cmd_gate_status(args: argparse.Namespace) -> int:
    """Check policy gate state for a gate_id, or list pending gates."""
    from agn.core.policy_gate import effective_gate_state, pending_gate_entries

    gate_id = (args.gate_id or "").strip()
    if gate_id:
        state = effective_gate_state(gate_id)
        _out({"ok": True, "gate_id": gate_id, **state})
    else:
        entries = pending_gate_entries()
        _out({"ok": True, "pending_count": len(entries), "entries": entries[:50]})
    return 0


def cmd_memory_search(args: argparse.Namespace) -> int:
    """Search memory records for a query string."""
    query = (args.query or "").strip().lower()
    scope = (args.scope or "all").strip()
    limit = max(1, min(100, args.limit))

    records_dir = ROOT / "memory" / "records"
    if not records_dir.is_dir():
        _out({"ok": True, "count": 0, "results": []})
        return 0

    results: list[dict[str, Any]] = []
    for scope_dir in sorted(records_dir.iterdir()):
        if not scope_dir.is_dir():
            continue
        if scope != "all" and scope_dir.name != scope:
            continue
        for f in scope_dir.glob("*.jsonl"):
            try:
                for line in f.read_text(encoding="utf-8", errors="replace").strip().splitlines():
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    searchable = json.dumps(entry, ensure_ascii=False).lower()
                    if query in searchable:
                        entry["_scope"] = scope_dir.name
                        results.append(entry)
                        if len(results) >= limit:
                            break
            except Exception:
                continue
        if len(results) >= limit:
            break

    results.sort(key=lambda e: str(e.get("ts", "")), reverse=True)
    _out({"ok": True, "query": query, "count": len(results), "results": results[:limit]})
    return 0


def cmd_artifact(args: argparse.Namespace) -> int:
    """Read an AGN artifact by pointer ref."""
    from pointer_protocol import parse_ref, read_ref_text, resolve_ref_path

    ref = args.ref
    try:
        parsed = parse_ref(ref)
        resolved = resolve_ref_path(ref)
        text = read_ref_text(ref, mode="tail", tail_lines=200, max_bytes=32768)
        _out({
            "ok": True,
            "ref": ref,
            "parsed": parsed,
            "path": str(resolved.relative_to(ROOT)),
            "bytes": resolved.stat().st_size,
            "text": text,
        })
        return 0
    except Exception as exc:
        _out({"ok": False, "error": str(exc)})
        return 1


# ── Write Commands (role-gated) ─────────────────────────────────────────


def cmd_dispatch(args: argparse.Namespace) -> int:
    """Submit a task via the dispatcher. Requires coordinator or admin role."""
    role = os.getenv("AGN_ROLE", "coordinator_agent").strip().lower()
    if role not in {"coordinator", "admin", "coordinator_agent"}:
        _out({"ok": False, "error": f"dispatch_not_permitted_for_role:{role}"})
        return 1

    try:
        payload = json.loads(args.json_payload)
    except Exception as exc:
        _out({"ok": False, "error": f"invalid_json:{exc}"})
        return 1

    if not isinstance(payload, dict):
        _out({"ok": False, "error": "payload_must_be_object"})
        return 1

    from dispatcher_runtime import dispatch_request
    result = dispatch_request(payload)
    _out({"ok": True, "dispatch_result": result})
    return 0


def cmd_record_memory(args: argparse.Namespace) -> int:
    """Record a cross-agent memory entry. Requires coordinator or admin role."""
    role = os.getenv("AGN_ROLE", "coordinator_agent").strip().lower()
    if role not in {"coordinator", "admin", "coordinator_agent"}:
        _out({"ok": False, "error": f"record_not_permitted_for_role:{role}"})
        return 1

    record = {
        "kind": args.kind,
        "summary": args.summary,
        "scope": args.scope,
        "author": args.author or os.getenv("AGN_AGENT_NAME", role),
        "confidence": args.confidence,
    }
    if args.task_id:
        record["task_id"] = args.task_id

    try:
        result = dispatch_memory_record(
            record,
            caller=f"agn_tool:{role}",
            task_id=str(args.task_id or "").strip(),
            trace_id="",
            intent="record_memory",
            reason="agn_tool record-memory",
            risk_level="low",
        )
        if not result.get("ok"):
            _out({"ok": False, "error": str(result.get("error", "record_memory_failed"))})
            return 1
        record_payload = result["record"]
        _out(
            {
                "ok": True,
                "record_id": record_payload["record_id"],
                "scope": record_payload["scope"],
                "dispatch_meta": result["dispatch_meta"],
            }
        )
        return 0
    except Exception as exc:
        _out({"ok": False, "error": str(exc)})
        return 1


def cmd_log_event(args: argparse.Namespace) -> int:
    """Append an audit event. Any agent can log events."""
    event_dir = ROOT / "reports" / "audit"
    event_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    event_path = event_dir / f"{day}.jsonl"

    event = {
        "ts": _utc_now(),
        "agent": args.agent or os.getenv("AGN_AGENT_NAME", os.getenv("AGN_ROLE", "unknown")),
        "action": args.action,
        "detail": args.detail or "",
        "severity": args.severity,
    }

    import fcntl
    with event_path.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(json.dumps(event, ensure_ascii=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    _out({"ok": True, "event": event})
    return 0


def cmd_whoami(_args: argparse.Namespace) -> int:
    """Show current agent identity and role context."""
    role = os.getenv("AGN_ROLE", "").strip() or "unset"
    agent_name = os.getenv("AGN_AGENT_NAME", "").strip() or "unknown"
    enforce_guard = os.getenv("AGN_ENFORCE_ROLE_GUARD", "").strip()

    # Load role permissions
    perms_path = ROOT / "config" / "role_permissions.json"
    perms: dict[str, Any] = {}
    if perms_path.exists():
        try:
            all_perms = json.loads(perms_path.read_text(encoding="utf-8"))
            perms = all_perms.get("roles", {}).get(role, {})
        except Exception:
            pass

    _out({
        "ok": True,
        "agent_name": agent_name,
        "role": role,
        "enforce_role_guard": enforce_guard or "0",
        "writable_dirs": perms.get("writable_dirs", []),
        "blocked_commands": len(perms.get("blocked_command_patterns", [])),
        "capabilities": _role_capabilities(role),
    })
    return 0


def _role_capabilities(role: str) -> list[str]:
    """Return human-readable capabilities for a role."""
    caps: dict[str, list[str]] = {
        "admin": ["read_all", "write_all", "dispatch", "record_memory", "emergency_control"],
        "coordinator": ["read_all", "dispatch", "record_memory", "write_ssot"],
        "coordinator_agent": ["read_all", "dispatch", "record_memory"],
        "executor": ["read_all", "write_results", "write_workspace"],
        "reviewer": ["read_all", "write_verdicts"],
    }
    return caps.get(role, ["read_all"])


# ── Composite Commands ──────────────────────────────────────────────────


def cmd_briefing(_args: argparse.Namespace) -> int:
    """Full orientation briefing: health + tasks summary + recent events.

    Designed for agent startup — one call gets everything needed to orient.
    """
    system_mode = _safe_json(ROOT / "runtime" / "admin_control" / "system_mode.json")
    lifecycle = _safe_json(ROOT / "runtime" / "admin_control" / "lifecycle" / "agn2_system.json")
    awakening = _safe_json(ROOT / "agn2" / "awakening" / "current_state.json")

    # Quick SSOT count
    ssot_dir = ROOT / "ssot"
    by_status: dict[str, int] = {}
    total = 0
    if ssot_dir.is_dir():
        from task_engine import derive_status
        from ssot_store import SSOTStore
        store = SSOTStore(ssot_dir)
        for f in ssot_dir.glob("*.json"):
            total += 1
            task = store.get_task(f.stem)
            if task:
                s = derive_status(task)
                by_status[s] = by_status.get(s, 0) + 1

    _out({
        "ok": True,
        "ts": _utc_now(),
        "system_mode": system_mode.get("mode", "unknown"),
        "emergency_stop_active": bool(system_mode.get("emergency_stop_active", False)),
        "dispatcher_active": bool(system_mode.get("dispatcher_accepts_new_work", False)),
        "lifecycle_status": lifecycle.get("status", "unknown"),
        "ssot": {"total": total, "by_status": by_status},
        "active_agents": awakening.get("active_agents", []),
        "scheduler": awakening.get("scheduler", {}),
        "awakening_generated_at": awakening.get("_meta", {}).get("generated_at", ""),
    })
    return 0


# ── Parser ──────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AGN Tool — unified CLI for agent access to AGN system state",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # Read-only
    sub.add_parser("health", help="System health snapshot").set_defaults(func=cmd_health)
    sub.add_parser("briefing", help="Full orientation briefing for agent startup").set_defaults(func=cmd_briefing)
    sub.add_parser("awakening", help="Read awakening state").set_defaults(func=cmd_awakening)
    sub.add_parser("providers", help="Provider availability").set_defaults(func=cmd_providers)

    tasks_p = sub.add_parser("tasks", help="List SSOT tasks")
    tasks_p.add_argument("--status", default="", help="Filter by status")
    tasks_p.add_argument("--limit", type=int, default=20, help="Max results")
    tasks_p.set_defaults(func=cmd_tasks)

    task_p = sub.add_parser("task", help="Get task detail")
    task_p.add_argument("task_id", help="Task ID")
    task_p.set_defaults(func=cmd_task)

    gate_p = sub.add_parser("gate-status", help="Policy gate status")
    gate_p.add_argument("gate_id", nargs="?", default="", help="Gate ID (omit to list pending)")
    gate_p.set_defaults(func=cmd_gate_status)

    mem_p = sub.add_parser("memory-search", help="Search memory records")
    mem_p.add_argument("query", help="Search query")
    mem_p.add_argument("--scope", default="all", help="Memory scope")
    mem_p.add_argument("--limit", type=int, default=10, help="Max results")
    mem_p.set_defaults(func=cmd_memory_search)

    art_p = sub.add_parser("artifact", help="Read artifact by pointer ref")
    art_p.add_argument("ref", help="AGN pointer ref (agn://...)")
    art_p.set_defaults(func=cmd_artifact)

    # Write (role-gated)
    disp_p = sub.add_parser("dispatch", help="Submit task via dispatcher (role-gated)")
    disp_p.add_argument("--json", dest="json_payload", required=True, help="JSON task payload")
    disp_p.set_defaults(func=cmd_dispatch)

    mem_w = sub.add_parser("record-memory", help="Record cross-agent memory entry (role-gated)")
    mem_w.add_argument("--kind", default="fact", choices=["fact", "decision", "todo", "constraint", "incident", "evidence", "status"])
    mem_w.add_argument("--summary", required=True, help="Memory summary text")
    mem_w.add_argument("--scope", default="global", help="Memory scope")
    mem_w.add_argument("--author", default="", help="Author agent name")
    mem_w.add_argument("--confidence", default="medium", choices=["low", "medium", "high"])
    mem_w.add_argument("--task-id", default="", help="Related task ID")
    mem_w.set_defaults(func=cmd_record_memory)

    log_p = sub.add_parser("log-event", help="Append an audit event")
    log_p.add_argument("--action", required=True, help="Event action name")
    log_p.add_argument("--detail", default="", help="Event detail text")
    log_p.add_argument("--agent", default="", help="Agent name (defaults to AGN_AGENT_NAME)")
    log_p.add_argument("--severity", default="info", choices=["debug", "info", "warning", "error", "critical"])
    log_p.set_defaults(func=cmd_log_event)

    sub.add_parser("whoami", help="Show current agent identity and role").set_defaults(func=cmd_whoami)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except Exception as exc:
        _out({"ok": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
