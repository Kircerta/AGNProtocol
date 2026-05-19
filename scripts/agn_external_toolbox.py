#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "agn2" / "awakening" / "external_toolbox.json"
DEFAULT_OPEN_SOURCE_ROOT = ROOT.parent / "OpenSource"


def _open_source_root() -> Path:
    configured = str(os.getenv("AGN_OPEN_SOURCE_ROOT", "")).strip()
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_OPEN_SOURCE_ROOT


def load_catalog(path: Path | None = None) -> dict[str, Any]:
    target = path or CATALOG_PATH
    return json.loads(target.read_text(encoding="utf-8"))


def _binary_status(name: str) -> dict[str, Any]:
    resolved = shutil.which(name) or ""
    return {
        "name": name,
        "available": bool(resolved),
        "path": resolved,
    }


def build_toolbox_entry(name: str, entry: dict[str, Any], *, open_source_root: Path | None = None) -> dict[str, Any]:
    root = open_source_root or _open_source_root()
    repo_path = root / str(entry.get("repo_dir", name))
    docs_relpath = str(entry.get("docs_relpath", "README.md"))
    docs_path = repo_path / docs_relpath
    binary_checks = [_binary_status(item) for item in entry.get("binary_checks", [])]
    binary_ready = all(item["available"] for item in binary_checks) if binary_checks else True
    repo_ready = repo_path.exists()
    if repo_ready and binary_ready:
        readiness = "available"
    elif repo_ready:
        readiness = "reference_only"
    else:
        readiness = "missing_repo"
    return {
        "name": name,
        "category": str(entry.get("category", "")),
        "mount_mode": str(entry.get("mount_mode", "reference_only")),
        "summary": str(entry.get("summary", "")),
        "repo_path": str(repo_path),
        "repo_exists": repo_ready,
        "docs_path": str(docs_path),
        "docs_exists": docs_path.exists(),
        "readiness": readiness,
        "binary_checks": binary_checks,
        "safe_capabilities": list(entry.get("safe_capabilities", [])),
        "agn_fit": list(entry.get("agn_fit", [])),
        "boundaries": list(entry.get("boundaries", [])),
        "preferred_surfaces": list(entry.get("preferred_surfaces", [])),
    }


def build_inventory(*, open_source_root: Path | None = None) -> dict[str, Any]:
    catalog = load_catalog()
    root = open_source_root or _open_source_root()
    entries = [
        build_toolbox_entry(name, entry, open_source_root=root)
        for name, entry in sorted(catalog.get("entries", {}).items())
    ]
    categories: dict[str, int] = {}
    for item in entries:
        categories[item["category"]] = categories.get(item["category"], 0) + 1
    return {
        "version": str(catalog.get("version", "")),
        "open_source_root": str(root),
        "count": len(entries),
        "categories": categories,
        "entries": entries,
    }


def show_entry(name: str, *, open_source_root: Path | None = None) -> dict[str, Any]:
    catalog = load_catalog()
    entries = catalog.get("entries", {})
    if name not in entries:
        raise KeyError(name)
    return build_toolbox_entry(name, entries[name], open_source_root=open_source_root or _open_source_root())


def _print_human_list(payload: dict[str, Any]) -> None:
    print(f"OpenSource root: {payload['open_source_root']}")
    for item in payload["entries"]:
        print(f"- {item['name']} [{item['category']}] {item['readiness']}: {item['summary']}")


def _print_human_show(payload: dict[str, Any]) -> None:
    print(f"{payload['name']} [{payload['category']}]")
    print(f"readiness: {payload['readiness']}")
    print(f"repo: {payload['repo_path']}")
    print(f"docs: {payload['docs_path']}")
    if payload["safe_capabilities"]:
        print("safe_capabilities:")
        for item in payload["safe_capabilities"]:
            print(f"- {item}")
    if payload["agn_fit"]:
        print("agn_fit:")
        for item in payload["agn_fit"]:
            print(f"- {item}")
    if payload["boundaries"]:
        print("boundaries:")
        for item in payload["boundaries"]:
            print(f"- {item}")
    if payload["preferred_surfaces"]:
        print("preferred_surfaces:")
        for item in payload["preferred_surfaces"]:
            print(f"- {item}")
    if payload["binary_checks"]:
        print("binary_checks:")
        for item in payload["binary_checks"]:
            state = "ok" if item["available"] else "missing"
            print(f"- {item['name']}: {state} {item['path']}".rstrip())


def cmd_list(args: argparse.Namespace) -> int:
    payload = build_inventory()
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        _print_human_list(payload)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    try:
        payload = show_entry(args.name)
    except KeyError:
        print(json.dumps({"ok": False, "error": "toolbox_entry_not_found", "name": args.name}, indent=2))
        return 1
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        _print_human_show(payload)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    if args.name:
        return cmd_show(args)
    return cmd_list(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect the curated AGN external toolbox mounts.")
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="List external toolbox entries")
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(func=cmd_list)

    show_parser = sub.add_parser("show", help="Show a specific toolbox entry")
    show_parser.add_argument("name")
    show_parser.add_argument("--json", action="store_true")
    show_parser.set_defaults(func=cmd_show)

    status_parser = sub.add_parser("status", help="Show toolbox status for all entries or one entry")
    status_parser.add_argument("name", nargs="?")
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(func=cmd_status)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
