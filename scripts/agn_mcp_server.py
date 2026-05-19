#!/usr/bin/env python3
"""AGN MCP Server — exposes AGN capabilities to any MCP-compatible agent.

Tools exposed:
  - agn_status:          System status snapshot (mode, health, SSOT summary)
  - agn_health:          Latest health check report
  - agn_task_get:        Get a specific SSOT task by ID
  - agn_task_list:       List SSOT tasks with optional status filter
  - agn_task_search:     Search tasks by correlation_id
  - agn_memory_query:    Query cross-agent memory records
  - agn_memory_search:   Semantic search over memory (requires ChromaDB)
  - agn_memory_write:    Write a new memory record
  - agn_tool_list:       List registered AGN tools
  - agn_tool_register:   Register a new tool (Toolmaker protocol)
  - agn_tool_run:        Run a registered tool by name
  - agn_admin_profile:   Read the admin profile
  - agn_audit_recent:    Recent admin audit trail entries
  - agn_emergency_status: Check if emergency stop is active
  - agn_scheduler_jobs:  List scheduled autonomous jobs and their status
  - agn_toolbox_list:    List curated external toolbox mounts
  - agn_toolbox_show:    Show one curated external toolbox mount
  - agn_toolbox_status:  Show readiness for external toolbox mounts

Usage:
  python scripts/agn_mcp_server.py            # stdio transport (for Claude Code)
  python scripts/agn_mcp_server.py --sse      # SSE transport (for web clients)
  python scripts/agn_mcp_server.py --port 9100 # custom SSE port

To register with Claude Code, add to .claude/settings.json:
  {
    "mcpServers": {
      "agn": {
        "command": "/path/to/.venv/bin/python",
        "args": ["/path/to/scripts/agn_mcp_server.py"]
      }
    }
  }
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
if str(ROOT / "agn_api") not in sys.path:
    sys.path.insert(0, str(ROOT / "agn_api"))

try:
    from agn_governed_execution import dispatch_memory_record
except ImportError:  # pragma: no cover
    from scripts.agn_governed_execution import dispatch_memory_record

# ── Helpers ──────────────────────────────────────────────────────────────

def _safe_json(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _safe_jsonl_tail(path: Path, n: int = 20) -> list[dict[str, Any]]:
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


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _toolbox_inventory() -> dict[str, Any]:
    try:
        from agn_external_toolbox import build_inventory
    except ImportError:
        from scripts.agn_external_toolbox import build_inventory
    return build_inventory()


def _toolbox_entry(name: str) -> dict[str, Any]:
    try:
        from agn_external_toolbox import show_entry
    except ImportError:
        from scripts.agn_external_toolbox import show_entry
    return show_entry(name)


# ── SSOT Store (lazy init) ──────────────────────────────────────────────

_store = None


def _get_store():
    global _store
    if _store is None:
        from agn_api.ssot_store import SSOTStore
        ssot_dir = ROOT / "ssot" / "tasks"
        if not ssot_dir.is_dir():
            ssot_dir = ROOT / ".agn_workspace" / "event_driven" / "ssot"
        _store = SSOTStore(ssot_dir)
    return _store


# ── Tool Registry (Toolmaker Protocol) ──────────────────────────────────

TOOL_REGISTRY_PATH = ROOT / "agn2" / "awakening" / "tool_registry.json"


def _load_tool_registry() -> dict[str, Any]:
    return _safe_json(TOOL_REGISTRY_PATH)


def _save_tool_registry(registry: dict[str, Any]) -> None:
    TOOL_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=".tool_registry.", suffix=".tmp", dir=TOOL_REGISTRY_PATH.parent)
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(registry, f, ensure_ascii=True, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, TOOL_REGISTRY_PATH)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


# ── ChromaDB (lazy init) ────────────────────────────────────────────────

_chroma_collection = None


def _get_chroma():
    global _chroma_collection
    if _chroma_collection is None:
        import chromadb
        chroma_dir = ROOT / "runtime" / "chromadb"
        chroma_dir.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(path=str(chroma_dir))
        _chroma_collection = client.get_or_create_collection(
            name="agn_memory",
            metadata={"description": "AGN cross-agent semantic memory"},
        )
    return _chroma_collection


# ── MCP Server ──────────────────────────────────────────────────────────

mcp = FastMCP(
    "AGN",
    instructions="AGN2.0 Agent Network — system tools for SSOT, memory, health, scheduling, and tool management",
)


# ── System Status Tools ─────────────────────────────────────────────────

@mcp.tool()
def agn_status() -> str:
    """Get AGN system status: mode, health, SSOT summary, active agents."""
    state = _safe_json(ROOT / "agn2" / "awakening" / "current_state.json")
    if not state:
        return json.dumps({"error": "awakening_daemon_not_running", "hint": "Run: launchctl load ~/Library/LaunchAgents/ai.agn.awakening.plist"})
    # Trim verbose fields for concise output
    state.pop("recent_audit", None)
    state.pop("recent_memory", None)
    return json.dumps(state, indent=2)


@mcp.tool()
def agn_health() -> str:
    """Get the latest system health check report."""
    report = _safe_json(ROOT / "reports" / "health" / "latest.json")
    if not report:
        return json.dumps({"error": "no_health_report", "hint": "Run: python scripts/agn_health_check.py --quick"})
    return json.dumps(report, indent=2)


@mcp.tool()
def agn_emergency_status() -> str:
    """Check if emergency stop is active. Returns mode and stop state."""
    mode = _safe_json(ROOT / "runtime" / "admin_control" / "system_mode.json")
    return json.dumps({
        "emergency_stop_active": bool(mode.get("emergency_stop_active", False)),
        "mode": mode.get("mode", "unknown"),
        "last_changed_by": mode.get("last_changed_by", ""),
        "last_reason": mode.get("last_reason", ""),
    }, indent=2)


@mcp.tool()
def agn_admin_profile() -> str:
    """Read the admin profile — preferences, communication style, safety boundaries."""
    local_profile = ROOT / "agn2" / "admin_profile.json"
    example_profile = ROOT / "agn2" / "admin_profile.example.json"
    profile = _safe_json(local_profile if local_profile.exists() else example_profile)
    if not profile:
        return json.dumps({"error": "admin_profile_not_found"})
    return json.dumps(profile, indent=2)


# ── SSOT Task Tools ─────────────────────────────────────────────────────

@mcp.tool()
def agn_task_get(task_id: str) -> str:
    """Get a specific SSOT task by its ID."""
    store = _get_store()
    task = store.get_task(task_id)
    if task is None:
        return json.dumps({"error": "task_not_found", "task_id": task_id})
    return json.dumps(task, indent=2)


@mcp.tool()
def agn_task_list(status_filter: str = "") -> str:
    """List SSOT tasks. Optionally filter by status (e.g., 'pending', 'running', 'done')."""
    store = _get_store()
    tasks = store.list_tasks()
    if status_filter:
        tasks = [t for t in tasks if str(t.get("status", "")).strip() == status_filter.strip()]
    # Return summary to avoid flooding context
    summaries = []
    for t in tasks[:50]:
        summaries.append({
            "id": t.get("id", ""),
            "status": t.get("status", ""),
            "correlation_id": t.get("correlation_id", ""),
            "created_at": t.get("created_at", ""),
        })
    return json.dumps({"count": len(tasks), "tasks": summaries}, indent=2)


@mcp.tool()
def agn_task_search(correlation_id: str) -> str:
    """Search for a task by correlation_id (O(1) indexed lookup)."""
    store = _get_store()
    task = store.get_task_by_correlation(correlation_id)
    if task is None:
        return json.dumps({"error": "not_found", "correlation_id": correlation_id})
    return json.dumps(task, indent=2)


# ── Memory Tools ─────────────────────────────────────────────────────────

@mcp.tool()
def agn_memory_query(
    author: str = "",
    kind: str = "",
    scope: str = "global",
    limit: int = 10,
    since: str = "",
) -> str:
    """Query cross-agent memory records. Filter by author, kind, scope, since (ISO date)."""
    try:
        from memory_recorder import query_agent_findings
        results = query_agent_findings(
            author=author or None,
            kind=kind or None,
            scope=scope or "global",
            limit=limit,
            since=since or None,
        )
        return json.dumps(results, indent=2, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)[:200]})


@mcp.tool()
def agn_memory_write(
    summary: str,
    kind: str = "fact",
    scope: str = "global",
    author: str = "agent",
    task_id: str = "",
    trace_id: str = "",
) -> str:
    """Write a new memory record. Kinds: fact, decision, todo, constraint, incident, evidence, status."""
    try:
        record = dispatch_memory_record(
            {
                "summary": summary,
                "kind": kind,
                "scope": scope,
                "author": author,
            },
            caller="agn_mcp_server",
            task_id=task_id or f"mcp-{utc_now()}",
            trace_id=trace_id or f"mcp-trace-{utc_now()}",
            intent="mcp_memory_write",
            reason="MCP memory append",
            risk_level="low",
        )
        if not record.get("ok"):
            return json.dumps({"ok": False, "error": str(record.get("error", "memory_write_failed"))[:200]})
        return json.dumps({"ok": True, "record_id": record["record"].get("record_id", ""), "dispatch_meta": record.get("dispatch_meta", {})})
    except Exception as exc:
        return json.dumps({"error": str(exc)[:200]})


@mcp.tool()
def agn_memory_search(query: str, n_results: int = 5) -> str:
    """Semantic search over AGN memory using ChromaDB vector store."""
    try:
        collection = _get_chroma()
        results = collection.query(query_texts=[query], n_results=min(n_results, 20))
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]
        output = []
        for i, doc in enumerate(docs):
            entry = {
                "text": doc[:500],
                "metadata": metas[i] if i < len(metas) else {},
                "distance": round(distances[i], 4) if i < len(distances) else None,
            }
            output.append(entry)
        return json.dumps(output, indent=2, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)[:200], "hint": "ChromaDB may need indexing. Run: python scripts/agn_mcp_server.py --index-memory"})


# ── Audit Tools ──────────────────────────────────────────────────────────

@mcp.tool()
def agn_audit_recent(n: int = 20) -> str:
    """Get the N most recent admin audit trail entries."""
    entries = _safe_jsonl_tail(ROOT / "runtime" / "admin_control" / "audit" / "admin_control.jsonl", n)
    return json.dumps(entries, indent=2, default=str)


@mcp.tool()
def agn_scheduler_jobs() -> str:
    """List scheduled autonomous jobs, their config, and last run times."""
    jobs_config = _safe_json(ROOT / "agn2" / "awakening" / "scheduler_jobs.json")
    last_runs = _safe_json(ROOT / "agn2" / "awakening" / "scheduler_last_run.json").get("last_runs", {})
    jobs = jobs_config.get("jobs", [])
    for job in jobs:
        name = job.get("name", "")
        job["last_run"] = last_runs.get(name, "never")
    return json.dumps({"jobs": jobs}, indent=2)


@mcp.tool()
def agn_toolbox_list() -> str:
    """List curated external toolbox mounts and their AGN fit."""
    payload = _toolbox_inventory()
    slim = []
    for item in payload.get("entries", []):
        slim.append(
            {
                "name": item.get("name", ""),
                "category": item.get("category", ""),
                "readiness": item.get("readiness", ""),
                "summary": item.get("summary", ""),
            }
        )
    return json.dumps({"count": len(slim), "entries": slim}, indent=2)


@mcp.tool()
def agn_toolbox_show(name: str) -> str:
    """Show a curated external toolbox mount with boundaries and preferred AGN surfaces."""
    try:
        return json.dumps(_toolbox_entry(name), indent=2)
    except KeyError:
        return json.dumps({"error": "toolbox_entry_not_found", "name": name}, indent=2)


@mcp.tool()
def agn_toolbox_status(name: str = "") -> str:
    """Show readiness for all toolbox mounts or for a named mount."""
    if name:
        return agn_toolbox_show(name)
    payload = _toolbox_inventory()
    return json.dumps(
        {
            "open_source_root": payload.get("open_source_root", ""),
            "count": payload.get("count", 0),
            "entries": [
                {
                    "name": item.get("name", ""),
                    "readiness": item.get("readiness", ""),
                    "repo_exists": item.get("repo_exists", False),
                    "docs_exists": item.get("docs_exists", False),
                }
                for item in payload.get("entries", [])
            ],
        },
        indent=2,
    )


# ── Toolmaker Protocol ──────────────────────────────────────────────────

@mcp.tool()
def agn_tool_list() -> str:
    """List all registered AGN tools (built-in + agent-created)."""
    registry = _load_tool_registry()
    tools = registry.get("tools", {})
    output = []
    for name, info in sorted(tools.items()):
        output.append({
            "name": name,
            "description": info.get("description", ""),
            "author": info.get("author", ""),
            "created_at": info.get("created_at", ""),
            "command": info.get("command", []),
        })
    return json.dumps({"count": len(output), "tools": output}, indent=2)


@mcp.tool()
def agn_tool_register(
    name: str,
    description: str,
    command: list[str],
    author: str = "agent",
    timeout_seconds: int = 60,
) -> str:
    """Register a new tool in the AGN toolbox. The command will be run from the repo root.

    Example: agn_tool_register(name="count_todos", description="Count open TODOs in codebase",
             command=["grep", "-r", "TODO", "scripts/", "--count"], author="claude")
    """
    if not name or not command:
        return json.dumps({"error": "name and command are required"})

    # Safety: check emergency stop before allowing tool registration
    mode = _safe_json(ROOT / "runtime" / "admin_control" / "system_mode.json")
    if bool(mode.get("emergency_stop_active", False)):
        return json.dumps({"error": "emergency_stop_active", "hint": "Cannot register tools during emergency stop"})

    # Safety: block obviously dangerous commands
    blocked = {"rm", "rmdir", "mkfs", "dd", "shutdown", "reboot", "kill", "killall"}
    cmd_base = Path(command[0]).name.lower() if command else ""
    if cmd_base in blocked:
        return json.dumps({"error": f"command '{cmd_base}' is blocked for safety"})

    registry = _load_tool_registry()
    if "tools" not in registry:
        registry["tools"] = {}

    registry["tools"][name] = {
        "description": description,
        "command": command,
        "author": author,
        "timeout_seconds": min(timeout_seconds, 300),
        "created_at": utc_now(),
    }
    _save_tool_registry(registry)
    return json.dumps({"ok": True, "name": name, "registered_at": utc_now()})


@mcp.tool()
def agn_tool_run(name: str, extra_args: list[str] | None = None) -> str:
    """Run a registered tool by name. Optionally pass extra arguments."""
    registry = _load_tool_registry()
    tool_info = registry.get("tools", {}).get(name)
    if not tool_info:
        return json.dumps({"error": f"tool '{name}' not found", "hint": "Use agn_tool_list to see available tools"})

    # Safety: check emergency stop
    mode = _safe_json(ROOT / "runtime" / "admin_control" / "system_mode.json")
    if bool(mode.get("emergency_stop_active", False)):
        return json.dumps({"error": "emergency_stop_active", "hint": "Cannot run tools during emergency stop"})

    command = list(tool_info["command"])
    if extra_args:
        command.extend(extra_args)
    try:
        timeout = min(int(tool_info.get("timeout_seconds", 60)), 300)
    except (ValueError, TypeError):
        timeout = 60

    try:
        proc = subprocess.run(
            command,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return json.dumps({
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "")[-2000:],
            "stderr": (proc.stderr or "")[-500:],
        })
    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"timeout after {timeout}s"})
    except Exception as exc:
        return json.dumps({"error": str(exc)[:200]})


# ── Memory Indexing ──────────────────────────────────────────────────────

def index_memory_to_chroma() -> dict[str, Any]:
    """Index all memory records + conversation archive summaries into ChromaDB."""
    collection = _get_chroma()
    indexed = 0

    # Index memory records
    records_dir = ROOT / "memory" / "records"
    if records_dir.is_dir():
        for scope_dir in records_dir.iterdir():
            if not scope_dir.is_dir():
                continue
            for f in scope_dir.glob("*.jsonl"):
                for entry in _safe_jsonl_tail(f, 1000):
                    summary = str(entry.get("summary", "")).strip()
                    if not summary or len(summary) < 10:
                        continue
                    record_id = str(entry.get("record_id", f"rec-{indexed}"))
                    try:
                        collection.upsert(
                            ids=[record_id],
                            documents=[summary],
                            metadatas=[{
                                "author": str(entry.get("author", "")),
                                "kind": str(entry.get("kind", "")),
                                "scope": scope_dir.name,
                                "ts": str(entry.get("ts", "")),
                                "source": "memory_record",
                            }],
                        )
                        indexed += 1
                    except Exception:
                        continue

    # Index research outputs
    research_dir = ROOT / "runtime" / "research_packets"
    if research_dir.is_dir():
        for f in research_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                summary = str(data.get("summary", data.get("title", ""))).strip()
                if summary and len(summary) >= 10:
                    collection.upsert(
                        ids=[f"research-{f.stem}"],
                        documents=[summary[:2000]],
                        metadatas=[{
                            "source": "research",
                            "ts": str(data.get("created_at", "")),
                        }],
                    )
                    indexed += 1
            except Exception:
                continue

    # Index audit trail (recent events as context)
    audit_entries = _safe_jsonl_tail(ROOT / "runtime" / "admin_control" / "audit" / "admin_control.jsonl", 200)
    for i, entry in enumerate(audit_entries):
        action = str(entry.get("action", "")).strip()
        if not action:
            continue
        text = f"{action}: {json.dumps({k: v for k, v in entry.items() if k not in ('ts', 'action')}, default=str)}"
        try:
            collection.upsert(
                ids=[f"audit-{i}-{entry.get('ts', '')}"],
                documents=[text[:1000]],
                metadatas=[{
                    "source": "audit",
                    "action": action,
                    "ts": str(entry.get("ts", "")),
                }],
            )
            indexed += 1
        except Exception:
            continue

    return {"indexed": indexed, "collection_count": collection.count()}


# ── Entry Point ──────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="AGN MCP Server")
    parser.add_argument("--sse", action="store_true", help="Use SSE transport instead of stdio")
    parser.add_argument("--port", type=int, default=9100, help="SSE port (default: 9100)")
    parser.add_argument("--index-memory", action="store_true", help="Index memory records into ChromaDB and exit")
    args = parser.parse_args()

    if args.index_memory:
        result = index_memory_to_chroma()
        print(json.dumps(result, indent=2))
        return

    if args.sse:
        mcp.run(transport="sse")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
