#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
REPO_SKILLS_ROOT = ROOT / "skills"

GROUPS = {
    "shared": {
        "source_root": REPO_SKILLS_ROOT / "shared",
        "target_root": Path.home() / ".codex" / "skills",
        "description": "General-purpose Codex skills installed under $CODEX_HOME/skills",
    },
    "agn": {
        "source_root": REPO_SKILLS_ROOT / "agn",
        "target_root": Path.home() / ".codex_agn" / "skills",
        "description": "AGN-specific skills installed under $AGN_CODEX_HOME/skills",
    },
}


def _repo_relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path)


def _home_relative(path: Path) -> str:
    home = Path.home().resolve()
    try:
        rel = path.resolve().relative_to(home)
        parts = rel.parts
        if parts and parts[0] == ".codex":
            suffix = "/".join(parts[1:])
            return "$CODEX_HOME" + (f"/{suffix}" if suffix else "")
        if parts and parts[0] == ".codex_agn":
            suffix = "/".join(parts[1:])
            return "$AGN_CODEX_HOME" + (f"/{suffix}" if suffix else "")
        return f"$HOME/{rel}"
    except ValueError:
        return str(path)


def _skill_dirs(source_root: Path) -> list[Path]:
    if not source_root.exists():
        return []
    return [
        entry
        for entry in sorted(source_root.iterdir())
        if entry.is_dir() and (entry / "SKILL.md").exists()
    ]


def build_inventory() -> dict[str, object]:
    groups: dict[str, object] = {}
    total = 0
    for name, meta in GROUPS.items():
        source_root = Path(meta["source_root"])
        target_root = Path(meta["target_root"])
        skills = []
        for skill_dir in _skill_dirs(source_root):
            skill_name = skill_dir.name
            installed_dir = target_root / skill_name
            skills.append(
                {
                    "name": skill_name,
                    "source_dir": str(skill_dir),
                    "target_dir": str(installed_dir),
                    "installed": installed_dir.exists(),
                    "has_redirect_note": (installed_dir / "AGN_CANONICAL_SOURCE.md").exists(),
                }
            )
        total += len(skills)
        groups[name] = {
            "description": meta["description"],
            "source_root": str(source_root),
            "target_root": str(target_root),
            "count": len(skills),
            "skills": skills,
        }
    return {
        "repo_root": str(ROOT),
        "skills_root": str(REPO_SKILLS_ROOT),
        "group_count": len(groups),
        "total_skill_count": total,
        "groups": groups,
    }


def _selected_groups(group: str) -> Iterable[tuple[str, dict[str, object]]]:
    if group == "all":
        return GROUPS.items()
    return [(group, GROUPS[group])]


def _selected_skill_names(names: list[str]) -> set[str]:
    return {item.strip() for item in names if item.strip()}


def _copy_skill(source_dir: Path, target_dir: Path) -> None:
    if target_dir.exists():
        subprocess.run(["rm", "-rf", str(target_dir)], check=True)
    shutil.copytree(source_dir, target_dir)


def _redirect_note(group_name: str, skill_name: str, source_dir: Path, target_dir: Path) -> str:
    return f"""# AGN Canonical Source

This installed skill is mirrored from the AGN repo.

- canonical source: `{_repo_relative(source_dir)}`
- installed copy: `{_home_relative(target_dir)}`
- group: `{group_name}`

From the AGN repo root, refresh this installed copy with:

```bash
python3 scripts/sync_repo_skills.py install --group {group_name} --skill {skill_name}
```

Repo operators should edit the canonical repo copy first, then sync it back into
the local skill homes.
"""


def cmd_list(args: argparse.Namespace) -> int:
    payload = build_inventory()
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    print(f"Repo skills root: {payload['skills_root']}")
    for group_name, group_payload in payload["groups"].items():
        print(f"\n[{group_name}] {group_payload['description']}")
        for skill in group_payload["skills"]:
            installed = "installed" if skill["installed"] else "missing_local_copy"
            print(f"- {skill['name']}: {installed}")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    selected = _selected_skill_names(args.skill)
    copied: list[dict[str, str]] = []
    for group_name, meta in _selected_groups(args.group):
        source_root = Path(meta["source_root"])
        target_root = Path(meta["target_root"])
        target_root.mkdir(parents=True, exist_ok=True)
        for source_dir in _skill_dirs(source_root):
            if selected and source_dir.name not in selected:
                continue
            target_dir = target_root / source_dir.name
            _copy_skill(source_dir, target_dir)
            copied.append(
                {
                    "group": group_name,
                    "skill": source_dir.name,
                    "source_dir": str(source_dir),
                    "target_dir": str(target_dir),
                }
            )
    print(json.dumps({"ok": True, "copied": copied, "count": len(copied)}, indent=2))
    return 0


def cmd_write_redirects(args: argparse.Namespace) -> int:
    selected = _selected_skill_names(args.skill)
    written: list[dict[str, str]] = []
    for group_name, meta in _selected_groups(args.group):
        source_root = Path(meta["source_root"])
        target_root = Path(meta["target_root"])
        for source_dir in _skill_dirs(source_root):
            if selected and source_dir.name not in selected:
                continue
            target_dir = target_root / source_dir.name
            if not target_dir.exists():
                continue
            note_path = target_dir / "AGN_CANONICAL_SOURCE.md"
            note_path.write_text(
                _redirect_note(group_name, source_dir.name, source_dir, target_dir),
                encoding="utf-8",
            )
            written.append(
                {
                    "group": group_name,
                    "skill": source_dir.name,
                    "note_path": str(note_path),
                }
            )
    print(json.dumps({"ok": True, "written": written, "count": len(written)}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync canonical repo skills into local Codex skill homes.")
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="List repo skills and whether local installs exist.")
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(func=cmd_list)

    install_parser = sub.add_parser("install", help="Install repo skills into the local Codex skill homes.")
    install_parser.add_argument("--group", choices=["all", "shared", "agn"], default="all")
    install_parser.add_argument("--skill", action="append", default=[])
    install_parser.set_defaults(func=cmd_install)

    redirect_parser = sub.add_parser("write-redirects", help="Write canonical-source notes into installed skill directories.")
    redirect_parser.add_argument("--group", choices=["all", "shared", "agn"], default="all")
    redirect_parser.add_argument("--skill", action="append", default=[])
    redirect_parser.set_defaults(func=cmd_write_redirects)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
