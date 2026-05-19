"""Tests for scripts/agn_tool.py — unified CLI for agent access."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGN_TOOL = ROOT / "scripts" / "agn_tool.py"


def _run(args: list[str], env: dict[str, str] | None = None) -> dict:
    """Run agn_tool.py with args, return parsed JSON output."""
    result = subprocess.run(
        [sys.executable, str(AGN_TOOL)] + args,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    return json.loads(result.stdout)


def test_health_returns_ok():
    out = _run(["health"])
    assert out["ok"] is True
    assert "system_mode" in out
    assert "emergency_stop_active" in out
    assert "health" in out


def test_briefing_returns_ssot_summary():
    out = _run(["briefing"])
    assert out["ok"] is True
    assert "ssot" in out
    assert "total" in out["ssot"]
    assert isinstance(out["ssot"]["by_status"], dict)


def test_tasks_returns_list():
    out = _run(["tasks", "--limit", "3"])
    assert out["ok"] is True
    assert isinstance(out["tasks"], list)
    assert out["count"] <= 3


def test_task_not_found():
    out = _run(["task", "nonexistent-task-id-xyz"])
    assert out["ok"] is False
    assert "not_found" in out["error"]


def test_awakening_returns_state():
    out = _run(["awakening"])
    # May be ok=True or ok=False depending on whether awakening was refreshed
    assert "ok" in out


def test_providers_returns_registry():
    out = _run(["providers"])
    assert out["ok"] is True
    assert "ts" in out


def test_memory_search_returns_results():
    out = _run(["memory-search", "nonexistent-query-xyz", "--limit", "5"])
    assert out["ok"] is True
    assert out["count"] == 0
    assert out["results"] == []


def test_gate_status_list():
    out = _run(["gate-status"])
    assert out["ok"] is True
    assert "pending_count" in out


def test_dispatch_blocked_for_unknown_role():
    """dispatch should be blocked for non-coordinator roles."""
    import os
    env = {**os.environ, "AGN_ROLE": "reviewer"}
    out = _run(["dispatch", "--json", '{"task_id": "test"}'], env=env)
    assert out["ok"] is False
    assert "not_permitted" in out["error"]


def test_whoami_returns_role_info():
    import os
    env = {**os.environ, "AGN_ROLE": "coordinator_agent", "AGN_AGENT_NAME": "natsura"}
    out = _run(["whoami"], env=env)
    assert out["ok"] is True
    assert out["role"] == "coordinator_agent"
    assert out["agent_name"] == "natsura"
    assert "read_all" in out["capabilities"]
    assert "dispatch" in out["capabilities"]


def test_record_memory_writes_entry():
    import os
    env = {**os.environ, "AGN_ROLE": "coordinator_agent", "AGN_AGENT_NAME": "test"}
    out = _run([
        "record-memory", "--kind", "fact",
        "--summary", "Test memory from agn_tool CLI",
        "--scope", "test", "--author", "test-agent",
    ], env=env)
    assert out["ok"] is True
    assert out["record_id"].startswith("mem-")
    assert out["scope"] == "test"


def test_record_memory_blocked_for_executor():
    import os
    env = {**os.environ, "AGN_ROLE": "executor"}
    out = _run([
        "record-memory", "--kind", "fact", "--summary", "Should fail",
    ], env=env)
    assert out["ok"] is False
    assert "not_permitted" in out["error"]


def test_log_event_writes_audit_entry():
    out = _run(["log-event", "--action", "cli_test", "--detail", "integration test", "--agent", "pytest"])
    assert out["ok"] is True
    assert out["event"]["action"] == "cli_test"
    assert out["event"]["agent"] == "pytest"
    assert out["event"]["severity"] == "info"
