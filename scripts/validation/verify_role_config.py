#!/usr/bin/env python3
"""Verify config/role_permissions.json is valid and complete.

Exit 0 on success, 1 on failure.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.schema_validate import validate_role_permissions


def main() -> int:
    config_path = ROOT / "config" / "role_permissions.json"
    if not config_path.exists():
        print(f"FAIL: config file not found: {config_path}", file=sys.stderr)
        return 1

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"FAIL: invalid JSON: {exc}", file=sys.stderr)
        return 1

    if not isinstance(data, dict):
        print("FAIL: top-level value must be a JSON object", file=sys.stderr)
        return 1

    errors = validate_role_permissions(data)

    # Additionally check that writable_dirs reference existing directories.
    roles = data.get("roles", {})
    for role_name, role_cfg in roles.items():
        if not isinstance(role_cfg, dict):
            continue
        for d in role_cfg.get("writable_dirs", []):
            if d == "*":
                continue
            dir_path = ROOT / d
            if not dir_path.is_dir():
                errors.append(f"roles.{role_name}.writable_dirs: directory does not exist: {d}")

    if errors:
        print(f"FAIL: {config_path}", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(f"OK: {config_path} (role_permissions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
