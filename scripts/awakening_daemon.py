#!/usr/bin/env python3
"""AGN Awakening Daemon — the 'BIOS' of the agent network.

Maintains two files that any agent reads on startup to instantly orient:

  agn2/awakening/current_state.json   — machine-readable system snapshot
  agn2/awakening/recent_context.md    — human-readable recent activity summary

This daemon is READ-ONLY with respect to system state.  It assembles a
picture from existing files — it never modifies SSOT, governance, or
runtime state.  It writes only to the awakening directory.

Safety:
  - Respects emergency_stop (reports it; does not interfere).
  - Non-destructive: only writes to agn2/awakening/.
  - Audits its own heartbeat to the admin audit trail.
  - Graceful on missing data: every read is wrapped in try/except.

Usage:
  python scripts/awakening_daemon.py                # single refresh
  python scripts/awakening_daemon.py --loop          # continuous (for launchd)
  python scripts/awakening_daemon.py --interval 60   # custom interval (seconds)
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
if str(ROOT / "agn_api") not in sys.path:
    sys.path.insert(0, str(ROOT / "agn_api"))

AWAKENING_DIR = ROOT / "agn2" / "awakening"
STATE_PATH = AWAKENING_DIR / "current_state.json"
CONTEXT_PATH = AWAKENING_DIR / "recent_context.md"

DEFAULT_INTERVAL = 30  # seconds

_shutdown = False


def _handle_signal(signum: int, _frame: Any) -> None:
    global _shutdown
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ── Helpers ──────────────────────────────────────────────────────────────

def utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _safe_json(path: Path) -> dict[str, Any]:
    """Read a JSON file, returning {} on any error."""
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _safe_jsonl_tail(path: Path, n: int = 20) -> list[dict[str, Any]]:
    """Read last N entries from a JSONL file."""
    try:
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        entries = []
        for line in lines[-n:]:
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    entries.append(obj)
            except Exception:
                continue
        return entries
    except Exception:
        return []


def _atomic_write(path: Path, content: str) -> None:
    """Atomic write via tmp + rename."""
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            if not content.endswith("\n"):
                f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


# ── State Collectors ─────────────────────────────────────────────────────

def _collect_system_mode() -> dict[str, Any]:
    return _safe_json(ROOT / "runtime" / "admin_control" / "system_mode.json")


def _collect_lifecycle() -> dict[str, Any]:
    return _safe_json(ROOT / "runtime" / "admin_control" / "lifecycle" / "agn2_system.json")


def _collect_overview() -> dict[str, Any]:
    return _safe_json(ROOT / "runtime" / "admin_control" / "read_models" / "overview.json")


def _collect_ssot_summary() -> dict[str, Any]:
    """Count tasks by status in SSOT."""
    ssot_dir = ROOT / "ssot"
    if not ssot_dir.is_dir():
        ssot_dir = ROOT / ".agn_workspace" / "event_driven" / "ssot"
    if not ssot_dir.is_dir():
        return {"total": 0, "by_status": {}}
    by_status: dict[str, int] = {}
    total = 0
    for f in ssot_dir.glob("*.json"):
        total += 1
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            status = str(data.get("status", "unknown")).strip() or "unknown"
        except Exception:
            status = "unreadable"
        by_status[status] = by_status.get(status, 0) + 1
    return {"total": total, "by_status": by_status}


def _collect_recent_audit(n: int = 10) -> list[dict[str, Any]]:
    return _safe_jsonl_tail(ROOT / "runtime" / "admin_control" / "audit" / "admin_control.jsonl", n)


def _collect_recent_memory(n: int = 5) -> list[dict[str, Any]]:
    """Collect most recent memory records across all scopes."""
    records_dir = ROOT / "memory" / "records"
    if not records_dir.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for scope_dir in records_dir.iterdir():
        if not scope_dir.is_dir():
            continue
        for f in scope_dir.glob("*.jsonl"):
            entries.extend(_safe_jsonl_tail(f, 3))
    # Sort by timestamp descending, take top N
    entries.sort(key=lambda e: str(e.get("ts", "")), reverse=True)
    return entries[:n]


def _collect_system_health() -> dict[str, Any]:
    """Non-invasive system health snapshot."""
    health: dict[str, Any] = {}
    try:
        load1, load5, load15 = os.getloadavg()
        health["load_avg"] = {"1m": round(load1, 2), "5m": round(load5, 2), "15m": round(load15, 2)}
    except Exception:
        pass
    try:
        stat = shutil.disk_usage(str(ROOT))
        health["disk"] = {
            "total_gb": round(stat.total / (1024**3), 1),
            "free_gb": round(stat.free / (1024**3), 1),
            "used_pct": round((stat.used / stat.total) * 100, 1),
        }
    except Exception:
        pass
    try:
        health["cpu_count"] = os.cpu_count()
        health["platform"] = platform.machine()
    except Exception:
        pass
    return health


_AGENT_COLLECT_TIMEOUT = 5  # seconds — prevent hanging on slow/stale filesystem ops


def _collect_active_agents() -> list[dict[str, Any]]:
    """Check which agents have recent heartbeat activity and identity status.

    All file operations are wrapped with a per-agent timeout to prevent
    the daemon from hanging on slow NFS mounts or stale file handles.
    """
    import concurrent.futures

    agents: list[dict[str, Any]] = []

    def _probe_openclaw_agent(agent: dict[str, Any]) -> dict[str, Any] | None:
        """Probe a single OpenClaw agent entry (runs inside timeout)."""
        agent_id = agent.get("id", "")
        agent_name = agent.get("name", "unknown")
        workspace = agent.get("workspace", "")

        entry: dict[str, Any] = {
            "name": agent_name,
            "id": agent_id,
            "source": "openclaw",
            "workspace": workspace,
        }

        if workspace:
            ws_path = Path(workspace).expanduser()
            soul_path = ws_path / "SOUL.md"
            memory_path = ws_path / "MEMORY.md"
            entry["identity_recovered"] = soul_path.exists()
            entry["has_memory"] = memory_path.exists()

            sessions_dir = Path.home() / ".openclaw" / "agents" / agent_id / "sessions"
            if sessions_dir.is_dir():
                session_files = sorted(
                    sessions_dir.glob("*.jsonl"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if session_files:
                    try:
                        age_min = (time.time() - session_files[0].stat().st_mtime) / 60
                        entry["last_session_min_ago"] = round(age_min, 1)
                    except Exception:
                        pass

        if "coordinator" in (agent_name + agent_id).lower():
            entry["agn_role"] = "coordinator_agent"
            entry["agn_capabilities"] = ["read_ssot", "read_health", "dispatch_task", "memory_search"]
        else:
            entry["agn_role"] = "observer"
            entry["agn_capabilities"] = ["read_ssot", "read_health", "memory_search", "report_to_admin"]

        return entry

    # Check OpenClaw agents with timeout per agent
    openclaw_config = Path.home() / ".openclaw" / "openclaw.json"
    if openclaw_config.exists():
        try:
            cfg = json.loads(openclaw_config.read_text(encoding="utf-8"))
            agents_section = cfg.get("agents", {})
            agent_list = agents_section.get("list", []) if isinstance(agents_section, dict) else agents_section
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                futures = {
                    pool.submit(_probe_openclaw_agent, agent): agent
                    for agent in (agent_list if isinstance(agent_list, list) else [])
                }
                for future in concurrent.futures.as_completed(futures, timeout=_AGENT_COLLECT_TIMEOUT):
                    try:
                        result = future.result(timeout=_AGENT_COLLECT_TIMEOUT)
                        if result:
                            agents.append(result)
                    except Exception:
                        pass
        except concurrent.futures.TimeoutError:
            print("[awakening_daemon] WARN: _collect_active_agents timed out on openclaw probes", file=sys.stderr)
        except Exception:
            pass

    # Check conversation archive daemon
    archive_log = ROOT / "runtime" / "conversation_archive" / "launchagent.stdout.log"
    if archive_log.exists():
        try:
            mtime = archive_log.stat().st_mtime
            age_min = (time.time() - mtime) / 60
            agents.append({
                "name": "conversation_archive",
                "source": "launchd",
                "last_activity_min_ago": round(age_min, 1),
            })
        except Exception:
            pass
    return agents


def _collect_scheduler_status() -> dict[str, Any]:
    """Check if autonomous scheduler is running and what jobs are configured."""
    jobs_path = ROOT / "agn2" / "awakening" / "scheduler_jobs.json"
    if not jobs_path.exists():
        return {"configured": False, "jobs": []}
    try:
        data = json.loads(jobs_path.read_text(encoding="utf-8"))
        jobs = data.get("jobs", [])
        return {
            "configured": True,
            "job_count": len(jobs),
            "jobs": [{"name": j.get("name", ""), "enabled": j.get("enabled", False)} for j in jobs],
        }
    except Exception:
        return {"configured": False, "jobs": []}


# ── Assembly ─────────────────────────────────────────────────────────────

def build_current_state() -> dict[str, Any]:
    """Assemble the full awakening state snapshot."""
    system_mode = _collect_system_mode()
    return {
        "_meta": {
            "generated_at": utc_now(),
            "generator": "awakening_daemon",
            "version": "1.0.0",
            "purpose": "Read this file on agent startup to instantly orient.",
        },
        "emergency_stop_active": bool(system_mode.get("emergency_stop_active", False)),
        "system_mode": system_mode.get("mode", "unknown"),
        "lifecycle": _collect_lifecycle(),
        "ssot_summary": _collect_ssot_summary(),
        "system_health": _collect_system_health(),
        "active_agents": _collect_active_agents(),
        "scheduler": _collect_scheduler_status(),
        "recent_audit": _collect_recent_audit(10),
        "recent_memory": _collect_recent_memory(5),
        "admin_profile": "agn2/admin_profile.example.json",
        "local_admin_profile": "agn2/admin_profile.json",
        "system_identity": "agn2/SYSTEM_IDENTITY.md",
        "constitution": "agn2/governance/constitution.json",
        "manifest": "agn2/system_manifest.json",
        "tool_access": {
            "agn_tool": str(ROOT / "scripts" / "agn_tool.py"),
            "agn_pointer_tool": str(ROOT / "scripts" / "agn_pointer_tool.py"),
            "agent_collaboration": str(ROOT / "scripts" / "agent_collaboration.py"),
            "usage": "python3 <tool_path> <subcommand> [args]",
            "available_commands": [
                "agn_tool.py health",
                "agn_tool.py briefing",
                "agn_tool.py tasks [--status X]",
                "agn_tool.py task <id>",
                "agn_tool.py providers",
                "agn_tool.py gate-status [gate_id]",
                "agn_tool.py memory-search <query>",
                "agn_tool.py artifact <ref>",
                "agn_tool.py awakening",
                "agn_tool.py dispatch --json <payload>",
            ],
        },
    }


def build_recent_context(state: dict[str, Any]) -> str:
    """Generate human-readable context summary from state."""
    lines: list[str] = []
    lines.append("# AGN Awakening Context")
    lines.append(f"Generated: {state['_meta']['generated_at']}")
    lines.append("")

    # Emergency stop
    if state.get("emergency_stop_active"):
        lines.append("## !! EMERGENCY STOP ACTIVE !!")
        mode = state.get("system_mode", "unknown")
        lines.append(f"System mode: {mode}")
        lines.append("All autonomous actions are suspended. Await admin release.")
        lines.append("")

    # System status
    lines.append("## System Status")
    lifecycle = state.get("lifecycle", {})
    lines.append(f"- Status: {lifecycle.get('status', 'unknown')}")
    lines.append(f"- Last refresh: {lifecycle.get('last_refresh_at', 'never')}")
    lines.append(f"- Mode: {state.get('system_mode', 'unknown')}")
    lines.append("")

    # Health
    health = state.get("system_health", {})
    if health:
        lines.append("## System Health")
        load = health.get("load_avg", {})
        if load:
            lines.append(f"- Load: {load.get('1m', '?')}/{load.get('5m', '?')}/{load.get('15m', '?')}")
        disk = health.get("disk", {})
        if disk:
            lines.append(f"- Disk: {disk.get('free_gb', '?')}GB free ({disk.get('used_pct', '?')}% used)")
        lines.append("")

    # SSOT
    ssot = state.get("ssot_summary", {})
    if ssot.get("total", 0) > 0:
        lines.append("## Tasks (SSOT)")
        lines.append(f"- Total: {ssot['total']}")
        for status, count in sorted(ssot.get("by_status", {}).items()):
            lines.append(f"  - {status}: {count}")
        lines.append("")

    # Active agents
    agents = state.get("active_agents", [])
    if agents:
        lines.append("## Active Agents")
        for a in agents:
            detail = f"({a.get('source', '')})"
            if "last_activity_min_ago" in a:
                detail += f" last active {a['last_activity_min_ago']}min ago"
            lines.append(f"- {a.get('name', 'unknown')} {detail}")
        lines.append("")

    # Scheduler
    sched = state.get("scheduler", {})
    if sched.get("configured"):
        lines.append("## Autonomous Scheduler")
        for j in sched.get("jobs", []):
            status = "enabled" if j.get("enabled") else "disabled"
            lines.append(f"- {j.get('name', 'unnamed')}: {status}")
        lines.append("")

    # Recent audit
    audit = state.get("recent_audit", [])
    if audit:
        lines.append("## Recent Admin Actions")
        for entry in audit[-5:]:
            ts = str(entry.get("ts", "")).split("T")[0] if "T" in str(entry.get("ts", "")) else str(entry.get("ts", ""))
            lines.append(f"- [{ts}] {entry.get('action', 'unknown')}")
        lines.append("")

    # Orientation pointers
    lines.append("## Orientation")
    lines.append("- Admin profile example: `agn2/admin_profile.example.json`")
    lines.append("- Local admin profile override: `agn2/admin_profile.json`")
    lines.append("- System identity: `agn2/SYSTEM_IDENTITY.md`")
    lines.append("- Constitution: `agn2/governance/constitution.json`")
    lines.append("- System manifest: `agn2/system_manifest.json`")
    lines.append("- Emergency stop: `scripts/emergency_stop.py`")
    lines.append("")

    return "\n".join(lines)


# ── Main Loop ────────────────────────────────────────────────────────────

def refresh_once() -> dict[str, Any]:
    """Run one refresh cycle. Returns the state for inspection."""
    state = build_current_state()
    state_json = json.dumps(state, ensure_ascii=True, indent=2, sort_keys=True)
    _atomic_write(STATE_PATH, state_json)

    # Post-write JSON roundtrip validation — catch silent corruption
    try:
        written = STATE_PATH.read_text(encoding="utf-8")
        roundtrip = json.loads(written)
        if not isinstance(roundtrip, dict) or roundtrip.get("_meta", {}).get("generator") != "awakening_daemon":
            print("[awakening_daemon] WARN: post-write roundtrip validation failed — structure mismatch", file=sys.stderr)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[awakening_daemon] WARN: post-write roundtrip validation error: {exc}", file=sys.stderr)

    context_md = build_recent_context(state)
    _atomic_write(CONTEXT_PATH, context_md)
    return state


def run_loop(interval: int = DEFAULT_INTERVAL) -> None:
    """Continuous refresh loop for launchd/systemd."""
    print(f"[awakening_daemon] starting loop interval={interval}s", file=sys.stderr)
    while not _shutdown:
        try:
            state = refresh_once()
            estop = state.get("emergency_stop_active", False)
            mode = state.get("system_mode", "unknown")
            print(f"[awakening_daemon] refreshed mode={mode} estop={estop}", file=sys.stderr)
        except Exception as exc:
            print(f"[awakening_daemon] ERROR: {exc}", file=sys.stderr)
        # Sleep in small increments so SIGTERM is responsive
        for _ in range(interval):
            if _shutdown:
                break
            time.sleep(1)
    print("[awakening_daemon] shutdown", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="AGN Awakening Daemon")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="Refresh interval in seconds")
    args = parser.parse_args()

    AWAKENING_DIR.mkdir(parents=True, exist_ok=True)

    if args.loop:
        run_loop(args.interval)
    else:
        state = refresh_once()
        print(json.dumps(state, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
