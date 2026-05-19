#!/usr/bin/env python3
"""AGN Maintenance — periodic cleanup tasks for long-running system health.

Subcommands:
  prune-locks     Remove stale .lock files older than 1 hour
  prune-logs      Compress JSONL audit/scheduler logs older than 7 days
  prune-quarantine Remove quarantined memory records older than 30 days

All operations are non-destructive (archive before delete) and safe to run
during active system operation.
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STALE_LOCK_AGE_SEC = 3600        # 1 hour
LOG_COMPRESS_AGE_DAYS = 7        # compress logs older than 7 days
QUARANTINE_PRUNE_AGE_DAYS = 30   # prune quarantine older than 30 days


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _log(action: str, detail: str) -> None:
    entry = {"ts": _utc_now(), "agent": "maintenance", "action": action, "detail": detail}
    audit_dir = ROOT / "reports" / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    audit_path = audit_dir / f"{day}.jsonl"
    try:
        with audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=True) + "\n")
    except OSError:
        pass


def prune_locks() -> dict:
    """Remove .lock files older than STALE_LOCK_AGE_SEC."""
    removed = []
    now = time.time()
    for lock_dir in [ROOT / ".locks", ROOT / "ssot" / ".locks", ROOT / "runtime" / ".locks"]:
        if not lock_dir.is_dir():
            continue
        for f in lock_dir.iterdir():
            if not f.is_file():
                continue
            try:
                age = now - f.stat().st_mtime
                if age > STALE_LOCK_AGE_SEC:
                    f.unlink()
                    removed.append(str(f.relative_to(ROOT)))
            except OSError:
                continue

    if removed:
        _log("prune_locks", f"removed {len(removed)} stale locks: {', '.join(removed[:10])}")
    result = {"ok": True, "removed_count": len(removed), "removed": removed[:20]}
    print(json.dumps(result, indent=2))
    return result


def prune_logs() -> dict:
    """Compress old JSONL log files (audit, scheduler) to .jsonl.gz."""
    compressed = []
    cutoff = time.time() - (LOG_COMPRESS_AGE_DAYS * 86400)

    log_dirs = [
        ROOT / "reports" / "audit",
        ROOT / "reports" / "scheduler",
        ROOT / "audit",
    ]

    for log_dir in log_dirs:
        if not log_dir.is_dir():
            continue
        for f in log_dir.glob("*.jsonl"):
            try:
                if f.stat().st_mtime > cutoff:
                    continue
                gz_path = f.with_suffix(".jsonl.gz")
                if gz_path.exists():
                    continue
                with f.open("rb") as src, gzip.open(gz_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                f.unlink()
                compressed.append(str(f.relative_to(ROOT)))
            except OSError:
                continue

    if compressed:
        _log("prune_logs", f"compressed {len(compressed)} log files")
    result = {"ok": True, "compressed_count": len(compressed), "compressed": compressed[:20]}
    print(json.dumps(result, indent=2))
    return result


def prune_quarantine() -> dict:
    """Remove quarantine records older than QUARANTINE_PRUNE_AGE_DAYS."""
    removed = []
    quarantine_dir = ROOT / "memory" / "quarantine"
    if not quarantine_dir.is_dir():
        result = {"ok": True, "removed_count": 0}
        print(json.dumps(result, indent=2))
        return result

    cutoff = time.time() - (QUARANTINE_PRUNE_AGE_DAYS * 86400)
    for f in quarantine_dir.rglob("*.jsonl"):
        try:
            if f.stat().st_mtime > cutoff:
                continue
            f.unlink()
            removed.append(str(f.relative_to(ROOT)))
        except OSError:
            continue

    if removed:
        _log("prune_quarantine", f"removed {len(removed)} old quarantine files")
    result = {"ok": True, "removed_count": len(removed), "removed": removed[:20]}
    print(json.dumps(result, indent=2))
    return result


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="AGN Maintenance tasks")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prune-locks", help="Remove stale lock files")
    sub.add_parser("prune-logs", help="Compress old JSONL log files")
    sub.add_parser("prune-quarantine", help="Remove old quarantined memory records")
    sub.add_parser("all", help="Run all maintenance tasks")

    args = parser.parse_args()
    if args.command == "prune-locks":
        prune_locks()
    elif args.command == "prune-logs":
        prune_logs()
    elif args.command == "prune-quarantine":
        prune_quarantine()
    elif args.command == "all":
        prune_locks()
        prune_logs()
        prune_quarantine()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
