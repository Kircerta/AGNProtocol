#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _forbidden_broad_roots() -> set[Path]:
    home = Path.home().resolve()
    values = {
        Path("/").resolve(),
        home,
        (home / "Documents").resolve(),
        ROOT.resolve(),
        Path("/Users").resolve(),
        Path("/Volumes").resolve(),
    }
    return values


def _delete_root_allowed(root: Path) -> tuple[bool, str]:
    resolved = root.expanduser().resolve()
    if resolved in _forbidden_broad_roots():
        return False, f"delete_root_too_broad:{resolved}"
    return True, ""


def plan_delete(*, root: Path, pattern: str, output: Path) -> int:
    allowed, reason = _delete_root_allowed(root)
    resolved_root = root.expanduser().resolve()
    matches: list[Path] = []
    total_bytes = 0
    if allowed and resolved_root.exists():
        for path in sorted(resolved_root.rglob(pattern)):
            if path.is_file():
                matches.append(path.resolve())
                total_bytes += path.stat().st_size
    payload = {
        "ok": allowed,
        "kind": "delete",
        "timestamp": utc_now_iso(),
        "dry_run": True,
        "root": str(resolved_root),
        "pattern": pattern,
        "match_count": len(matches),
        "total_bytes": total_bytes,
        "matches": [str(item) for item in matches],
        "guardrail_status": "ready_for_confirmation" if allowed else "blocked",
        "errors": [] if allowed else [reason],
        "next_step": "review plan and confirm before any deletion command",
    }
    _write_json(output, payload)
    print(json.dumps({"ok": payload["ok"], "output": str(output)}, ensure_ascii=True))
    return 0 if allowed else 1


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def plan_rename(*, manifest_path: Path, output: Path) -> int:
    payload = _load_manifest(manifest_path)
    operations = payload.get("operations", [])
    errors: list[str] = []
    preview: list[dict[str, Any]] = []
    seen_targets: set[Path] = set()
    if not isinstance(operations, list) or not operations:
        errors.append("rename_manifest_missing_operations")
    else:
        for idx, op in enumerate(operations):
            if not isinstance(op, dict):
                errors.append(f"operations[{idx}]_must_be_object")
                continue
            src_raw = str(op.get("from", "")).strip()
            dst_raw = str(op.get("to", "")).strip()
            if not src_raw or not dst_raw:
                errors.append(f"operations[{idx}]_missing_from_or_to")
                continue
            src = Path(src_raw).expanduser().resolve()
            dst = Path(dst_raw).expanduser().resolve()
            if not src.exists():
                errors.append(f"source_missing:{src}")
            if src == dst:
                errors.append(f"rename_noop:{src}")
            if dst in seen_targets:
                errors.append(f"target_collision:{dst}")
            if dst.exists() and dst != src:
                errors.append(f"target_exists:{dst}")
            seen_targets.add(dst)
            preview.append({"from": str(src), "to": str(dst)})
    ready = not errors
    result = {
        "ok": ready,
        "kind": "batch_rename",
        "timestamp": utc_now_iso(),
        "dry_run": True,
        "manifest_path": str(manifest_path.resolve()),
        "operation_count": len(preview),
        "preview": preview,
        "guardrail_status": "ready_for_confirmation" if ready else "blocked",
        "errors": errors,
        "next_step": "review mapping and confirm before any rename command",
    }
    _write_json(output, result)
    print(json.dumps({"ok": result["ok"], "output": str(output)}, ensure_ascii=True))
    return 0 if ready else 1


def plan_config_change(*, target_path: Path, backup_path: Path, change_summary: str, output: Path) -> int:
    target = target_path.expanduser().resolve()
    backup = backup_path.expanduser().resolve()
    errors: list[str] = []
    if target == backup:
        errors.append("backup_path_must_differ_from_target")
    if not str(change_summary).strip():
        errors.append("change_summary_required")
    result = {
        "ok": not errors,
        "kind": "system_config_change",
        "timestamp": utc_now_iso(),
        "dry_run": True,
        "target_path": str(target),
        "target_exists": target.exists(),
        "backup_path": str(backup),
        "change_summary": change_summary.strip(),
        "requires_backup": True,
        "requires_diff_review": True,
        "guardrail_status": "ready_for_confirmation" if not errors else "blocked",
        "errors": errors,
        "next_step": "create backup and review diff before editing the config file",
    }
    _write_json(output, result)
    print(json.dumps({"ok": result["ok"], "output": str(output)}, ensure_ascii=True))
    return 0 if not errors else 1


def plan_publish(*, repo_path: Path, remote: str, branch: str, files: list[str], allow_external_publish: bool, admin_approved: bool, output: Path) -> int:
    repo = repo_path.expanduser().resolve()
    errors: list[str] = []
    if not repo.exists() or not repo.is_dir():
        errors.append(f"repo_missing:{repo}")
    elif not (repo / ".git").exists():
        errors.append(f"repo_not_git:{repo}")
    if not remote.strip():
        errors.append("remote_required")
    if not branch.strip():
        errors.append("branch_required")
    if not allow_external_publish:
        errors.append("allow_external_publish_not_set")
    if not admin_approved:
        errors.append("admin_approved_not_set")
    result = {
        "ok": not errors,
        "kind": "external_publish",
        "timestamp": utc_now_iso(),
        "dry_run": True,
        "repo_path": str(repo),
        "remote": remote.strip(),
        "branch": branch.strip(),
        "files": [str(item).strip() for item in files if str(item).strip()],
        "allow_external_publish": allow_external_publish,
        "admin_approved": admin_approved,
        "guardrail_status": "ready_for_confirmation" if not errors else "blocked",
        "errors": errors,
        "next_step": "review publish target, changed files, and approvals before any push or send action",
    }
    _write_json(output, result)
    print(json.dumps({"ok": result["ok"], "output": str(output)}, ensure_ascii=True))
    return 0 if not errors else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dry-run guardrails for high-risk actions")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_delete = sub.add_parser("plan-delete")
    p_delete.add_argument("--root", required=True)
    p_delete.add_argument("--pattern", required=True)
    p_delete.add_argument("--output", required=True)

    p_rename = sub.add_parser("plan-rename")
    p_rename.add_argument("--manifest", required=True)
    p_rename.add_argument("--output", required=True)

    p_config = sub.add_parser("plan-config-change")
    p_config.add_argument("--target-path", required=True)
    p_config.add_argument("--backup-path", required=True)
    p_config.add_argument("--change-summary", required=True)
    p_config.add_argument("--output", required=True)

    p_publish = sub.add_parser("plan-publish")
    p_publish.add_argument("--repo-path", required=True)
    p_publish.add_argument("--remote", required=True)
    p_publish.add_argument("--branch", required=True)
    p_publish.add_argument("--file", action="append", default=[])
    p_publish.add_argument("--allow-external-publish", action="store_true")
    p_publish.add_argument("--admin-approved", action="store_true")
    p_publish.add_argument("--output", required=True)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.cmd == "plan-delete":
        return plan_delete(root=Path(args.root), pattern=str(args.pattern), output=Path(args.output))
    if args.cmd == "plan-rename":
        return plan_rename(manifest_path=Path(args.manifest), output=Path(args.output))
    if args.cmd == "plan-config-change":
        return plan_config_change(
            target_path=Path(args.target_path),
            backup_path=Path(args.backup_path),
            change_summary=str(args.change_summary),
            output=Path(args.output),
        )
    return plan_publish(
        repo_path=Path(args.repo_path),
        remote=str(args.remote),
        branch=str(args.branch),
        files=list(args.file or []),
        allow_external_publish=bool(args.allow_external_publish),
        admin_approved=bool(args.admin_approved),
        output=Path(args.output),
    )


if __name__ == "__main__":
    raise SystemExit(main())
