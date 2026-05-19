"""Tests for scripts/agn_maintenance.py — periodic cleanup tasks."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAINTENANCE = ROOT / "scripts" / "agn_maintenance.py"


def _run(args: list[str]) -> dict:
    result = subprocess.run(
        [sys.executable, str(MAINTENANCE)] + args,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def test_prune_locks_runs_cleanly():
    out = _run(["prune-locks"])
    assert out["ok"] is True
    assert isinstance(out["removed_count"], int)


def test_prune_locks_removes_stale_lock(tmp_path: Path, monkeypatch):
    """Create a stale lock file and verify it gets cleaned up."""
    lock_dir = tmp_path / ".locks"
    lock_dir.mkdir()
    stale = lock_dir / "test.lock"
    stale.write_text("locked")
    # Set mtime to 2 hours ago
    old_time = time.time() - 7200
    os.utime(stale, (old_time, old_time))

    # Patch ROOT in maintenance module
    monkeypatch.setattr("scripts.agn_maintenance.ROOT", tmp_path)
    sys.path.insert(0, str(ROOT))
    import importlib
    import scripts.agn_maintenance as m
    importlib.reload(m)
    monkeypatch.setattr(m, "ROOT", tmp_path)

    result = m.prune_locks()
    assert result["removed_count"] >= 1
    assert not stale.exists()


def test_prune_logs_runs_cleanly():
    out = _run(["prune-logs"])
    assert out["ok"] is True
    assert isinstance(out["compressed_count"], int)


def test_prune_quarantine_runs_cleanly():
    out = _run(["prune-quarantine"])
    assert out["ok"] is True
    assert isinstance(out["removed_count"], int)


def test_all_subcommand_runs():
    result = subprocess.run(
        [sys.executable, str(MAINTENANCE), "all"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    # Should produce 3 JSON outputs (one per task)
    lines = result.stdout.strip().split("\n")
    json_blocks = []
    current = []
    for line in lines:
        current.append(line)
        try:
            json.loads("\n".join(current))
            json_blocks.append("\n".join(current))
            current = []
        except json.JSONDecodeError:
            continue
    assert len(json_blocks) == 3
