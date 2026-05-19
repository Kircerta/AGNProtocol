#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory_sync import MEMORY_DIR, merge_memory_events, load_all_memory_events, resolve_instance_id, write_conflicts_snapshot


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def run_git(cmd: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git command failed: {' '.join(cmd)} :: {proc.stderr.strip()}")
    return proc


def log(msg: str) -> None:
    print(f"[agn_git_sync] {utc_now_iso()} {msg}", flush=True)


def current_branch() -> str:
    proc = run_git(["git", "rev-parse", "--abbrev-ref", "HEAD"], check=True)
    return proc.stdout.strip()


def ahead_behind(branch: str) -> tuple[int, int]:
    proc = run_git(["git", "rev-list", "--left-right", "--count", f"HEAD...origin/{branch}"], check=True)
    raw = proc.stdout.strip().split()
    if len(raw) != 2:
        return 0, 0
    ahead = int(raw[0] or 0)
    behind = int(raw[1] or 0)
    return ahead, behind


def ensure_git_repo() -> None:
    proc = run_git(["git", "rev-parse", "--git-dir"])
    if proc.returncode != 0:
        raise RuntimeError("current directory is not a git repository")


def auto_commit_memory(instance_id: str) -> bool:
    if not MEMORY_DIR.exists():
        return False

    run_git(["git", "add", "-A", "--", str(MEMORY_DIR.relative_to(ROOT))], check=True)
    staged = run_git(["git", "diff", "--cached", "--name-only"])
    if not staged.stdout.strip():
        return False

    msg = f"chore(sync): memory sync from {instance_id}"
    commit = run_git(["git", "commit", "-m", msg])
    if commit.returncode != 0:
        log(f"memory commit skipped rc={commit.returncode} stderr={commit.stderr.strip()}")
        return False
    log(f"committed memory changes: {msg}")
    return True


def sync_memory_conflicts(instance_id: str) -> None:
    merged = merge_memory_events(load_all_memory_events())
    path = write_conflicts_snapshot(merged.get("conflicts", []), instance_id=instance_id)
    log(
        "memory merge complete "
        f"events={merged.get('total_events', 0)} keys={merged.get('distinct_keys', 0)} "
        f"conflicts={len(merged.get('conflicts', []))} snapshot={path.relative_to(ROOT)}"
    )


def run_once(*, do_pull: bool, do_push: bool, do_commit_memory: bool) -> int:
    ensure_git_repo()
    instance_id = resolve_instance_id()
    branch = current_branch()

    fetch = run_git(["git", "fetch", "origin", branch])
    if fetch.returncode != 0:
        log(f"fetch failed rc={fetch.returncode} stderr={fetch.stderr.strip()}")
        return 1

    ahead, behind = ahead_behind(branch)
    log(f"branch={branch} ahead={ahead} behind={behind}")

    if do_pull and behind > 0:
        pull = run_git(["git", "pull", "--rebase", "--autostash", "origin", branch])
        if pull.returncode != 0:
            log(f"pull failed rc={pull.returncode} stderr={pull.stderr.strip()}")
            return 1
        log("pull completed")

    sync_memory_conflicts(instance_id)

    if do_commit_memory:
        auto_commit_memory(instance_id)

    ahead_after, _ = ahead_behind(branch)
    if do_push and ahead_after > 0:
        push = run_git(["git", "push", "origin", branch])
        if push.returncode != 0:
            log(f"push failed rc={push.returncode} stderr={push.stderr.strip()}")
            return 1
        log("push completed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AGN git sync daemon (startup pull + periodic push)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_preflight = sub.add_parser("preflight")
    p_preflight.add_argument("--no-pull", action="store_true")
    p_preflight.add_argument("--no-push", action="store_true")
    p_preflight.add_argument("--no-commit-memory", action="store_true")

    p_once = sub.add_parser("once")
    p_once.add_argument("--no-pull", action="store_true")
    p_once.add_argument("--no-push", action="store_true")
    p_once.add_argument("--no-commit-memory", action="store_true")

    p_loop = sub.add_parser("loop")
    p_loop.add_argument("--interval-seconds", type=float, default=300.0)
    p_loop.add_argument("--no-pull", action="store_true")
    p_loop.add_argument("--no-push", action="store_true")
    p_loop.add_argument("--no-commit-memory", action="store_true")
    return parser


def _resolve_flags(args: argparse.Namespace) -> tuple[bool, bool, bool]:
    do_pull = not bool(args.no_pull)
    do_push = not bool(args.no_push)
    do_commit_memory = not bool(args.no_commit_memory)
    return do_pull, do_push, do_commit_memory


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    do_pull, do_push, do_commit_memory = _resolve_flags(args)

    if args.cmd in {"preflight", "once"}:
        return run_once(do_pull=do_pull, do_push=do_push, do_commit_memory=do_commit_memory)

    interval = max(10.0, float(args.interval_seconds))
    while True:
        rc = run_once(do_pull=do_pull, do_push=do_push, do_commit_memory=do_commit_memory)
        if rc != 0:
            log(f"sync cycle failed rc={rc}")
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
