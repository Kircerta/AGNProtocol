#!/usr/bin/env python3
"""AGN Autonomous Scheduler — runs background tasks on the Example Workstation.

Reads job definitions from agn2/awakening/scheduler_jobs.json and executes
them on their configured schedules.  Designed to run as a launchd daemon.

Safety guarantees:
  1. ALWAYS checks emergency_stop before executing any job.
  2. Jobs are NON-DESTRUCTIVE by default (read-only or constructive).
  3. Every execution is audited to reports/scheduler/audit.jsonl.
  4. System load is checked before each job — skips if overloaded.
  5. Job failures are logged and reported, never retried in a tight loop.
  6. Maximum concurrent jobs = 1 (sequential execution prevents storms).

Usage:
  python scripts/agn_scheduler.py                   # single tick
  python scripts/agn_scheduler.py --loop             # continuous daemon
  python scripts/agn_scheduler.py --interval 60      # custom tick interval
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
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

JOBS_PATH = ROOT / "agn2" / "awakening" / "scheduler_jobs.json"
AUDIT_PATH = ROOT / "reports" / "scheduler" / "audit.jsonl"
LAST_RUN_PATH = ROOT / "agn2" / "awakening" / "scheduler_last_run.json"

DEFAULT_INTERVAL = 60  # seconds between ticks
MAX_LOAD_AVG = 8.0  # skip jobs if 1-min load > this
MAX_JOB_TIMEOUT = 900  # 15 minutes max per job

_shutdown = False


def _handle_signal(signum: int, _frame: Any) -> None:
    global _shutdown
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def utc_now_dt() -> datetime:
    return datetime.now(tz=timezone.utc)


def _safe_json(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _append_audit(entry: dict[str, Any]) -> None:
    """Append an audit entry to the scheduler log."""
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with AUDIT_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps({**entry, "ts": utc_now()}, ensure_ascii=True, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception as exc:
        import sys as _sys
        print(f"[agn_scheduler] audit write failed: {exc}", file=_sys.stderr)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=True, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


# ── Safety Checks ────────────────────────────────────────────────────────

def is_emergency_stop() -> bool:
    mode = _safe_json(ROOT / "runtime" / "admin_control" / "system_mode.json")
    return bool(mode.get("emergency_stop_active", False))


def is_system_overloaded() -> bool:
    try:
        load1, _, _ = os.getloadavg()
        return load1 > MAX_LOAD_AVG
    except Exception:
        return False


# ── Job Execution ────────────────────────────────────────────────────────

def load_jobs() -> list[dict[str, Any]]:
    data = _safe_json(JOBS_PATH)
    jobs = data.get("jobs", [])
    return [j for j in jobs if isinstance(j, dict)]


def load_last_runs() -> dict[str, str]:
    """Map of job_name -> ISO timestamp of last execution."""
    data = _safe_json(LAST_RUN_PATH)
    return data.get("last_runs", {})


def save_last_runs(last_runs: dict[str, str]) -> None:
    _atomic_write_json(LAST_RUN_PATH, {"last_runs": last_runs})


def is_job_due(job: dict[str, Any], last_runs: dict[str, str]) -> bool:
    """Check if a job should run based on its interval."""
    name = job.get("name", "")
    if not job.get("enabled", False):
        return False
    try:
        interval_sec = int(job.get("interval_seconds", 3600))
    except (ValueError, TypeError):
        interval_sec = 3600
    last_run_str = last_runs.get(name, "")
    if not last_run_str:
        return True
    try:
        last_run = datetime.fromisoformat(last_run_str)
        elapsed = (utc_now_dt() - last_run).total_seconds()
        return elapsed >= interval_sec
    except Exception:
        return True


def execute_job(job: dict[str, Any]) -> dict[str, Any]:
    """Execute a single job. Returns result dict."""
    name = job.get("name", "unnamed")
    command = job.get("command", [])
    try:
        timeout = min(int(job.get("timeout_seconds", MAX_JOB_TIMEOUT)), MAX_JOB_TIMEOUT)
    except (ValueError, TypeError):
        timeout = MAX_JOB_TIMEOUT

    if not command:
        return {"name": name, "ok": False, "error": "no_command"}

    _append_audit({"action": "job_start", "job": name, "command": command})

    try:
        proc = subprocess.run(
            command,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        result = {
            "name": name,
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout_tail": (proc.stdout or "")[-500:],
            "stderr_tail": (proc.stderr or "")[-500:],
        }
    except subprocess.TimeoutExpired:
        result = {"name": name, "ok": False, "error": f"timeout_after_{timeout}s"}
    except Exception as exc:
        result = {"name": name, "ok": False, "error": str(exc)[:200]}

    _append_audit({"action": "job_complete", "job": name, "ok": result["ok"],
                    "error": result.get("error", "")})
    return result


# ── Tick ─────────────────────────────────────────────────────────────────

def tick() -> list[dict[str, Any]]:
    """Run one scheduler tick. Returns list of job results."""
    # Safety gate 1: emergency stop
    if is_emergency_stop():
        _append_audit({"action": "tick_skipped", "reason": "emergency_stop"})
        print("[agn_scheduler] emergency stop active — skipping tick", file=sys.stderr)
        return []

    # Safety gate 2: system load
    if is_system_overloaded():
        _append_audit({"action": "tick_skipped", "reason": "system_overloaded"})
        print("[agn_scheduler] system overloaded — skipping tick", file=sys.stderr)
        return []

    jobs = load_jobs()
    if not jobs:
        return []

    last_runs = load_last_runs()
    results: list[dict[str, Any]] = []

    for job in jobs:
        if _shutdown:
            break
        if not is_job_due(job, last_runs):
            continue

        name = job.get("name", "unnamed")
        print(f"[agn_scheduler] executing: {name}", file=sys.stderr)

        # Re-check emergency stop before each job (it could activate mid-tick)
        if is_emergency_stop():
            _append_audit({"action": "job_skipped", "job": name, "reason": "emergency_stop"})
            break

        result = execute_job(job)
        results.append(result)

        # Record execution time regardless of success/failure
        last_runs[name] = utc_now()
        save_last_runs(last_runs)

    return results


# ── Main Loop ────────────────────────────────────────────────────────────

def run_loop(interval: int = DEFAULT_INTERVAL) -> None:
    print(f"[agn_scheduler] starting loop interval={interval}s", file=sys.stderr)
    _append_audit({"action": "daemon_start", "interval": interval})

    while not _shutdown:
        try:
            results = tick()
            if results:
                ok = sum(1 for r in results if r.get("ok"))
                fail = len(results) - ok
                print(f"[agn_scheduler] tick done: {ok} ok, {fail} failed", file=sys.stderr)
        except Exception as exc:
            print(f"[agn_scheduler] ERROR: {exc}", file=sys.stderr)
            _append_audit({"action": "tick_error", "error": str(exc)[:200]})

        for _ in range(interval):
            if _shutdown:
                break
            time.sleep(1)

    _append_audit({"action": "daemon_stop"})
    print("[agn_scheduler] shutdown", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="AGN Autonomous Scheduler")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="Tick interval in seconds")
    args = parser.parse_args()

    if args.loop:
        run_loop(args.interval)
    else:
        results = tick()
        print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
