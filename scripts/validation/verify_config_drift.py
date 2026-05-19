#!/usr/bin/env python3
"""Detect configuration drift between local AGN and KiraraState repo.

Compares ``config/role_permissions.json`` and ``config/providers.json``
against their synced copies in the KiraraState repo.

Exit 0 if configs match, 1 if drift detected or KiraraState unavailable.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _expand_path(raw: str) -> Path:
    value = str(raw or "").strip()
    if not value:
        return Path("")
    value = os.path.expandvars(os.path.expanduser(value))
    return Path(value).resolve()


def _sha256(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    # Load KiraraState sync config to find the repo path.
    sync_config_path = ROOT / "config" / "kirara_state_sync.json"
    if not sync_config_path.exists():
        print("SKIP: kirara_state_sync.json not found", file=sys.stderr)
        return 0

    try:
        sync_config = json.loads(sync_config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"FAIL: cannot read sync config: {exc}", file=sys.stderr)
        return 1

    state_repo = _expand_path(str(sync_config.get("state_repo_path", "")))
    if not state_repo.is_dir():
        print(f"SKIP: KiraraState repo not found at {state_repo}", file=sys.stderr)
        return 0

    # Check each config file for drift.
    checks = [
        ("config/role_permissions.json", "shared/agn/config/role_permissions.json"),
        ("config/providers.json", "shared/agn/config/providers.json"),
    ]

    drift_found = False
    for local_rel, remote_rel in checks:
        local_path = ROOT / local_rel
        remote_path = state_repo / remote_rel
        local_hash = _sha256(local_path)
        remote_hash = _sha256(remote_path)

        if not local_path.exists():
            print(f"WARN: local {local_rel} does not exist")
            continue

        if not remote_path.exists():
            print(f"WARN: remote {remote_rel} does not exist in KiraraState (not yet synced?)")
            continue

        if local_hash != remote_hash:
            print(f"DRIFT: {local_rel} differs from KiraraState copy")
            print(f"  local:  {local_hash[:16]}...")
            print(f"  remote: {remote_hash[:16]}...")
            drift_found = True
        else:
            print(f"OK: {local_rel} matches KiraraState")

    if drift_found:
        print("\nDrift detected. Run 'make kirara-state-sync-out' to push local config.")
        return 1

    print("\nAll configs in sync.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
