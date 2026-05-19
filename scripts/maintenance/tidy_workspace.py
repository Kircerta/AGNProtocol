#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[2]

# Runtime surfaces that should never carry source-of-truth code.
RUNTIME_DIRS = [
    "dispatch",
    "results",
    "verdicts",
    "reports",
    "ssot",
    "audit",
    "runtime",
    "dead_letter",
    ".pytest_cache",
]

EMPTY_DIR_PRUNE_ROOTS = [
    ".agn_workspace",
    "memory/events",
    "memory/state",
    "memory/instances",
    "memory/conflicts",
]


@dataclass
class Removal:
    path: Path
    reason: str


def _tracked_files_under(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        out = subprocess.check_output(
            ["git", "ls-files", "--", str(path)],
            cwd=str(ROOT),
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _iter_memory_runtime_files() -> Iterable[Path]:
    # Keep the git-synced skeleton and only purge machine/runtime payloads.
    for pat in (
        "memory/events/*/*.jsonl",
        "memory/state/*.json",
        "memory/instances/*.json",
        "memory/conflicts/*.json",
    ):
        yield from ROOT.glob(pat)


def _iter_cache_noise() -> Iterable[tuple[Path, str]]:
    for path in ROOT.rglob("__pycache__"):
        if path.is_dir():
            yield path, "pycache_dir"
    for path in ROOT.rglob(".DS_Store"):
        if path.is_file():
            yield path, "ds_store"


def _iter_empty_runtime_dirs(*, include_workspace: bool) -> Iterable[Path]:
    for rel in EMPTY_DIR_PRUNE_ROOTS:
        root = ROOT / rel
        if not root.exists() or not root.is_dir():
            continue
        for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if not path.is_dir():
                continue
            if any(path.iterdir()):
                continue
            if _tracked_files_under(path):
                continue
            yield path
        if root.name == ".agn_workspace" and not include_workspace:
            continue
        if not any(root.iterdir()) and not _tracked_files_under(root):
            yield root


def _plan_removals(*, include_workspace: bool) -> list[Removal]:
    plan: list[Removal] = []

    for rel in RUNTIME_DIRS:
        path = ROOT / rel
        if not path.exists():
            continue
        tracked = _tracked_files_under(path)
        if tracked:
            continue
        plan.append(Removal(path=path, reason="runtime_surface"))

    for path in _iter_memory_runtime_files():
        if not path.exists() or not path.is_file():
            continue
        tracked = _tracked_files_under(path)
        if tracked:
            continue
        plan.append(Removal(path=path, reason="memory_runtime_file"))

    if include_workspace:
        ws = ROOT / ".agn_workspace"
        if ws.exists() and not _tracked_files_under(ws):
            plan.append(Removal(path=ws, reason="workspace_cache"))

    for path, reason in _iter_cache_noise():
        if _tracked_files_under(path):
            continue
        plan.append(Removal(path=path, reason=reason))

    for path in _iter_empty_runtime_dirs(include_workspace=include_workspace):
        plan.append(Removal(path=path, reason="runtime_empty_dir"))

    # De-duplicate while preserving first-seen reason.
    uniq: dict[Path, Removal] = {}
    for item in plan:
        uniq.setdefault(item.path, item)
    plan = list(uniq.values())

    return plan


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean AGN runtime/generated workspace clutter.")
    parser.add_argument("--apply", action="store_true", help="Apply removals. Default is dry-run.")
    parser.add_argument(
        "--include-workspace",
        action="store_true",
        help="Also remove .agn_workspace (default keeps it).",
    )
    args = parser.parse_args()

    plan = _plan_removals(include_workspace=bool(args.include_workspace))
    print(f"root={ROOT}")
    print(f"apply={bool(args.apply)} include_workspace={bool(args.include_workspace)}")
    print(f"planned_removals={len(plan)}")

    for item in plan:
        rel = item.path.relative_to(ROOT)
        print(f"- {rel} [{item.reason}]")

    if not args.apply:
        return 0

    for item in plan:
        _remove(item.path)

    print("status=done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
