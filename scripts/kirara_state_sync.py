#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.memory_sync import resolve_instance_id, _sanitize_instance_id  # type: ignore

OPENCLAW_ROOT = Path(
    os.path.expandvars(
        os.path.expanduser(str(os.getenv("OPENCLAW_ROOT", str(Path.home() / ".openclaw"))))
    )
).resolve()
DEFAULT_AGN_WORKSPACE_ROOT = Path(
    os.path.expandvars(
        os.path.expanduser(str(os.getenv("AGN_WORKSPACE_ROOT", str(ROOT))))
    )
).resolve()
DEFAULT_STATE_REPO_PATH = Path(
    os.path.expandvars(
        os.path.expanduser(
            str(os.getenv("KIRARA_STATE_REPO_PATH", str(Path.home() / "Documents" / "KiraraState")))
        )
    )
).resolve()
DEFAULT_CONFIG_PATH = ROOT / "config" / "kirara_state_sync.json"
DEFAULT_MERGED_OUTPUT = ROOT / "runtime" / "kirara_memory_merged.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_git(cwd: Path, args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def remote_heads(repo_path: Path) -> list[str]:
    proc = run_git(repo_path, ["ls-remote", "--heads", "origin"])
    if proc.returncode != 0:
        return []
    heads: list[str] = []
    for line in proc.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) != 2:
            continue
        ref = parts[1]
        if ref.startswith("refs/heads/"):
            heads.append(ref.split("/", 2)[-1])
    return heads


def resolve_sync_branch(repo_path: Path) -> str:
    heads = remote_heads(repo_path)
    if "main" in heads:
        return "main"
    if "master" in heads:
        return "master"
    if heads:
        return heads[0]
    return "main"


def log(msg: str) -> None:
    print(f"[kirara_state_sync] {utc_now_iso()} {msg}", flush=True)


def _expand_path(value: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("empty path")
    defaults = {
        "HOME": str(Path.home()),
        "OPENCLAW_ROOT": str(OPENCLAW_ROOT),
        "AGN_WORKSPACE_ROOT": str(DEFAULT_AGN_WORKSPACE_ROOT),
    }
    for key, fallback in defaults.items():
        resolved = str(os.getenv(key, fallback) or fallback).strip() or fallback
        raw = raw.replace(f"${{{key}}}", resolved).replace(f"${key}", resolved)
    return Path(os.path.expandvars(os.path.expanduser(raw))).resolve()


def _copy_path(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
        return True
    shutil.copy2(src, dst)
    return True


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_or_create_config(path: Path, instance_id: str) -> dict[str, Any]:
    if path.exists():
        data = _read_json(path)
        if data:
            return data

    default = {
        "state_repo_path": "$HOME/Documents/KiraraState",
        "state_repo_url": "",
        "shared_paths": [
            {
                "source": "$OPENCLAW_ROOT/workspace/AGENTS.md",
                "target": "shared/openclaw/workspace/AGENTS.md",
            },
            {
                "source": "$OPENCLAW_ROOT/workspace/IDENTITY.md",
                "target": "shared/openclaw/workspace/IDENTITY.md",
            },
            {
                "source": "$OPENCLAW_ROOT/workspace/SOUL.md",
                "target": "shared/openclaw/workspace/SOUL.md",
            },
            {
                "source": "$OPENCLAW_ROOT/workspace/USER.md",
                "target": "shared/openclaw/workspace/USER.md",
            },
            {
                "source": "$OPENCLAW_ROOT/workspace/HEARTBEAT.md",
                "target": "shared/openclaw/workspace/HEARTBEAT.md",
            },
            {
                "source": "$OPENCLAW_ROOT/workspace/memory",
                "target": "shared/openclaw/workspace/memory",
            },
            {
                "source": "$AGN_WORKSPACE_ROOT/memory",
                "target": "shared/agn/memory",
            },
            {
                "source": "$AGN_WORKSPACE_ROOT/dispatch",
                "target": "shared/agn/dispatch",
            },
            {
                "source": "$AGN_WORKSPACE_ROOT/results",
                "target": "shared/agn/results",
            },
            {
                "source": "$AGN_WORKSPACE_ROOT/verdicts",
                "target": "shared/agn/verdicts",
            },
            {
                "source": "$AGN_WORKSPACE_ROOT/runtime/kirara_memory_merged.json",
                "target": "shared/agn/runtime/kirara_memory_merged.json",
            },
            {
                "source": "$AGN_WORKSPACE_ROOT/runtime/provider_capabilities.json",
                "target": "shared/agn/runtime/provider_capabilities.json",
            },
        ],
        "device_paths": [
            {
                "source": "$AGN_WORKSPACE_ROOT/runtime/kirara_tasks.json",
                "target": "devices/{instance_id}/agn/runtime/kirara_tasks.json",
            },
            {
                "source": "$AGN_WORKSPACE_ROOT/runtime/kirara_heartbeat_state.json",
                "target": "devices/{instance_id}/agn/runtime/kirara_heartbeat_state.json",
            },
            {
                "source": "$OPENCLAW_ROOT/openclaw.json",
                "target": "devices/{instance_id}/openclaw/openclaw.json",
            },
            {
                "source": "$OPENCLAW_ROOT/logs/config-audit.jsonl",
                "target": "devices/{instance_id}/openclaw/logs/config-audit.jsonl",
            },
        ],
        "ops_log_target": "devices/{instance_id}/ops/sync_log.jsonl",
        "instance_id": instance_id,
    }
    _write_json(path, default)
    return default


def _resolve_repo_path(cfg: dict[str, Any]) -> Path:
    env_path = str(os.getenv("KIRARA_STATE_REPO_PATH", "")).strip()
    if env_path:
        return _expand_path(env_path)
    cfg_path = str(cfg.get("state_repo_path", "")).strip()
    if cfg_path:
        return _expand_path(cfg_path)
    return DEFAULT_STATE_REPO_PATH


def _resolve_repo_url(cfg: dict[str, Any]) -> str:
    env_url = str(os.getenv("KIRARA_STATE_REPO_URL", "")).strip()
    if env_url:
        return env_url
    return str(cfg.get("state_repo_url", "")).strip()


def ensure_state_repo(repo_path: Path, repo_url: str) -> None:
    if (repo_path / ".git").exists():
        return
    if repo_path.exists() and any(repo_path.iterdir()):
        raise RuntimeError(f"state repo path exists and is not a git repo: {repo_path}")
    if not repo_url:
        raise RuntimeError("state repo missing; provide --repo-url or KIRARA_STATE_REPO_URL")
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(["git", "clone", repo_url, str(repo_path)], text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git clone failed: {proc.stderr.strip()}")


def _iter_mappings(cfg: dict[str, Any], instance_id: str, mode: str) -> list[tuple[Path, str]]:
    key = "shared_paths" if mode == "shared" else "device_paths"
    rows: list[tuple[Path, str]] = []
    for item in cfg.get(key, []):
        if not isinstance(item, dict):
            continue
        src = str(item.get("source", "")).strip()
        target = str(item.get("target", "")).strip()
        if not src or not target:
            continue
        rows.append((_expand_path(src), target.format(instance_id=instance_id)))
    return rows


def export_to_state_repo(cfg: dict[str, Any], repo_path: Path, instance_id: str) -> dict[str, int]:
    copied = 0
    skipped = 0
    for mode in ("shared", "device"):
        for src, target in _iter_mappings(cfg, instance_id, mode):
            dst = (repo_path / target).resolve()
            if _copy_path(src, dst):
                copied += 1
            else:
                skipped += 1
    return {"copied": copied, "skipped": skipped}


def _is_ssot_dir(path: Path) -> bool:
    """Check if a path is in the SSOT directory."""
    try:
        ssot_dir = ROOT / "ssot"
        return ssot_dir.resolve() in path.resolve().parents or path.resolve() == ssot_dir.resolve()
    except Exception:
        return False


def _merge_ssot_task(local_path: Path, remote_path: Path) -> bool:
    """Merge SSOT task JSON: prefer the copy with a non-null decision,
    or the one with the later reviewed_at/created_at timestamp.
    Returns True if any write happened."""
    local_data = _read_json(local_path) if local_path.exists() else {}
    remote_data = _read_json(remote_path) if remote_path.exists() else {}

    if not remote_data:
        return False
    if not local_data:
        _copy_path(remote_path, local_path)
        return True

    # If one has a decision and the other doesn't, keep the one with a decision.
    local_decision = local_data.get("decision")
    remote_decision = remote_data.get("decision")

    if remote_decision and not local_decision:
        _copy_path(remote_path, local_path)
        return True
    if local_decision and not remote_decision:
        return False  # local is more advanced

    # Both have decisions or neither does — use latest timestamp.
    local_ts = str(local_data.get("reviewed_at") or local_data.get("created_at") or "")
    remote_ts = str(remote_data.get("reviewed_at") or remote_data.get("created_at") or "")
    if remote_ts > local_ts:
        _copy_path(remote_path, local_path)
        return True

    return False


def import_from_state_repo(cfg: dict[str, Any], repo_path: Path, instance_id: str) -> dict[str, int]:
    copied = 0
    skipped = 0
    merged = 0
    for mode in ("shared", "device"):
        for local_src, target in _iter_mappings(cfg, instance_id, mode):
            src = (repo_path / target).resolve()
            dst = local_src

            # P1-7 fix: for SSOT files, merge instead of blind overwrite.
            if _is_ssot_dir(dst) and dst.is_file() and src.is_file() and dst.suffix == ".json":
                if _merge_ssot_task(dst, src):
                    merged += 1
                else:
                    skipped += 1
            elif _is_ssot_dir(dst) and src.is_dir():
                # For SSOT directory, merge each task file individually.
                if src.exists():
                    dst.mkdir(parents=True, exist_ok=True)
                    for remote_file in src.glob("*.json"):
                        local_file = dst / remote_file.name
                        if _merge_ssot_task(local_file, remote_file):
                            merged += 1
                        else:
                            skipped += 1
                else:
                    skipped += 1
            elif _copy_path(src, dst):
                copied += 1
            else:
                skipped += 1
    return {"copied": copied, "skipped": skipped, "merged": merged}


def append_ops_log(cfg: dict[str, Any], repo_path: Path, instance_id: str) -> Path:
    target = str(cfg.get("ops_log_target", "devices/{instance_id}/ops/sync_log.jsonl"))
    path = repo_path / target.format(instance_id=instance_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    agn_head = run_git(ROOT, ["rev-parse", "--short", "HEAD"])
    state_head = run_git(repo_path, ["rev-parse", "--short", "HEAD"])
    event = {
        "ts": utc_now_iso(),
        "instance_id": instance_id,
        "hostname": socket.gethostname(),
        "agn_head": agn_head.stdout.strip() if agn_head.returncode == 0 else "",
        "state_head_before_commit": state_head.stdout.strip() if state_head.returncode == 0 else "",
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")
    return path


def commit_and_push(repo_path: Path, instance_id: str, do_push: bool) -> dict[str, Any]:
    run_git(repo_path, ["add", "-A"], check=True)
    staged = run_git(repo_path, ["diff", "--cached", "--name-only"])
    changed = [line.strip() for line in staged.stdout.splitlines() if line.strip()]
    if not changed:
        return {"committed": False, "pushed": False, "changed_files": 0}

    msg = f"chore(sync): kirara state update from {instance_id}"
    run_git(repo_path, ["commit", "-m", msg], check=True)
    pushed = False
    if do_push:
        branch = resolve_sync_branch(repo_path)
        heads = remote_heads(repo_path)
        if branch in heads:
            run_git(repo_path, ["fetch", "origin", branch], check=True)
            run_git(repo_path, ["rebase", f"origin/{branch}"], check=True)
            run_git(repo_path, ["push", "origin", branch], check=True)
        else:
            run_git(repo_path, ["push", "-u", "origin", f"HEAD:{branch}"], check=True)
        pushed = True
    return {"committed": True, "pushed": pushed, "changed_files": len(changed)}


def pull_latest(repo_path: Path) -> None:
    heads = remote_heads(repo_path)
    if not heads:
        return
    branch = resolve_sync_branch(repo_path)
    run_git(repo_path, ["fetch", "origin", branch], check=True)
    run_git(repo_path, ["rebase", f"origin/{branch}"], check=True)


def merge_local_memory() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "memory_sync.py"),
            "merge",
            "--output",
            str(DEFAULT_MERGED_OUTPUT),
        ],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"memory merge failed: {proc.stderr.strip() or proc.stdout.strip()}")


def cmd_init(args: argparse.Namespace) -> int:
    instance_id = _sanitize_instance_id(args.instance_id or resolve_instance_id())
    cfg = load_or_create_config(Path(args.config), instance_id)
    if args.repo_url:
        cfg["state_repo_url"] = str(args.repo_url).strip()
    if args.repo_path:
        cfg["state_repo_path"] = str(_expand_path(args.repo_path))
    _write_json(Path(args.config), cfg)

    repo_path = _resolve_repo_path(cfg)
    repo_url = _resolve_repo_url(cfg)
    ensure_state_repo(repo_path, repo_url)
    log(f"initialized config={args.config} repo={repo_path}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    instance_id = _sanitize_instance_id(args.instance_id or resolve_instance_id())
    cfg = load_or_create_config(Path(args.config), instance_id)
    repo_path = _resolve_repo_path(cfg)
    payload = {
        "ok": True,
        "instance_id": instance_id,
        "config": str(Path(args.config).resolve()),
        "repo_path": str(repo_path),
        "repo_exists": (repo_path / ".git").exists(),
        "shared_paths": len(cfg.get("shared_paths", [])),
        "device_paths": len(cfg.get("device_paths", [])),
    }
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def cmd_sync_in(args: argparse.Namespace) -> int:
    instance_id = _sanitize_instance_id(args.instance_id or resolve_instance_id())
    cfg = load_or_create_config(Path(args.config), instance_id)
    repo_path = _resolve_repo_path(cfg)
    repo_url = _resolve_repo_url(cfg)
    ensure_state_repo(repo_path, repo_url)

    if not args.no_pull:
        pull_latest(repo_path)
    imported = import_from_state_repo(cfg, repo_path, instance_id)
    if not args.no_merge_memory:
        merge_local_memory()
    print(json.dumps({"ok": True, "action": "sync-in", "imported": imported}, ensure_ascii=True))
    return 0


def cmd_sync_out(args: argparse.Namespace) -> int:
    instance_id = _sanitize_instance_id(args.instance_id or resolve_instance_id())
    cfg = load_or_create_config(Path(args.config), instance_id)
    repo_path = _resolve_repo_path(cfg)
    repo_url = _resolve_repo_url(cfg)
    ensure_state_repo(repo_path, repo_url)

    exported = export_to_state_repo(cfg, repo_path, instance_id)
    ops = append_ops_log(cfg, repo_path, instance_id)
    commit = commit_and_push(repo_path, instance_id, do_push=not args.no_push)
    print(
        json.dumps(
            {
                "ok": True,
                "action": "sync-out",
                "exported": exported,
                "ops_log": str(ops),
                "commit": commit,
            },
            ensure_ascii=True,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Sync shared Kirara/OpenClaw state across machines")
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    p.add_argument("--instance-id", default="")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Create config and clone KiraraState repo")
    p_init.add_argument("--repo-url", default="")
    p_init.add_argument("--repo-path", default="")
    p_init.set_defaults(func=cmd_init)

    p_status = sub.add_parser("status", help="Show sync configuration status")
    p_status.set_defaults(func=cmd_status)

    p_in = sub.add_parser("sync-in", help="Pull KiraraState and import to local machine")
    p_in.add_argument("--no-pull", action="store_true")
    p_in.add_argument("--no-merge-memory", action="store_true")
    p_in.set_defaults(func=cmd_sync_in)

    p_out = sub.add_parser("sync-out", help="Export local state and push KiraraState")
    p_out.add_argument("--no-push", action="store_true")
    p_out.set_defaults(func=cmd_sync_out)
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except Exception as exc:
        log(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
