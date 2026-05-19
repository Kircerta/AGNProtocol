#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


HOME = Path.home()
SOURCE = HOME / ".codex"
TARGET = HOME / ".codex_agn"


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _safe_unlink(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()


def _ensure_copy_if_missing(name: str) -> None:
    src = SOURCE / name
    dst = TARGET / name
    if src.exists() and not dst.exists():
        dst.write_bytes(src.read_bytes())


def _ensure_symlink(src: Path, dst: Path) -> dict[str, Any]:
    if not src.exists():
        return {"target": str(dst), "status": "missing_source", "source": str(src)}
    if dst.is_symlink():
        current = os.path.realpath(dst)
        if current == str(src.resolve()):
            return {"target": str(dst), "status": "already_linked", "source": str(src)}
        dst.unlink()
    elif dst.exists():
        return {"target": str(dst), "status": "skipped_existing", "source": str(src)}
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.symlink_to(src)
    return {"target": str(dst), "status": "linked", "source": str(src)}


def main() -> int:
    _ensure_dir(TARGET)
    for name in ("shell_snapshots", "sessions", "log", "tmp", "rules"):
        _ensure_dir(TARGET / name)

    for name in ("auth.json", "config.toml", "AGENTS.md"):
        _ensure_copy_if_missing(name)

    links: list[dict[str, Any]] = []
    for name in ("MACHINE_CONTEXT.md", "RECENT_MACHINE_SETUP.md"):
        links.append(_ensure_symlink(SOURCE / name, TARGET / name))

    _ensure_dir(TARGET / "skills")
    links.append(_ensure_symlink(SOURCE / "skills" / "TOOLBOX.md", TARGET / "skills" / "TOOLBOX.md"))

    source_skills = SOURCE / "skills"
    if source_skills.exists():
        for item in sorted(source_skills.iterdir()):
            if item.name in {".system", "TOOLBOX.md"}:
                continue
            links.append(_ensure_symlink(item, TARGET / "skills" / item.name))

    payload = {
        "ok": True,
        "source": str(SOURCE),
        "target": str(TARGET),
        "links": links,
    }
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
