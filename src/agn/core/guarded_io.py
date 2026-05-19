"""AGN guarded write helpers.

This is the real package implementation for AGN's role-guarded filesystem
write helpers. The legacy script remains only as a compatibility shim.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from agn.core.role_guard import require_write_access


PACKAGE_PATH = "agn.core.guarded_io"
LEGACY_SCRIPT_SHIM = "scripts/guarded_io.py"


def safe_mkdir(path: Path, *, task_id: str | None = None) -> None:
    target = Path(path)
    require_write_access(target, task_id=task_id)
    target.mkdir(parents=True, exist_ok=True)


def write_text(
    path: Path,
    content: str,
    *,
    append: bool = False,
    encoding: str = "utf-8",
    task_id: str | None = None,
) -> None:
    target = Path(path)
    require_write_access(target, task_id=task_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with target.open(mode, encoding=encoding) as handle:
        handle.write(content)


def write_bytes(
    path: Path,
    content: bytes,
    *,
    append: bool = False,
    task_id: str | None = None,
) -> None:
    target = Path(path)
    require_write_access(target, task_id=task_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = "ab" if append else "wb"
    with target.open(mode) as handle:
        handle.write(content)


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8", task_id: str | None = None) -> None:
    target = Path(path)
    require_write_access(target, task_id=task_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def atomic_write_bytes(path: Path, content: bytes, *, task_id: str | None = None) -> None:
    target = Path(path)
    require_write_access(target, task_id=task_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def atomic_write_json(path: Path, payload: dict[str, Any], *, task_id: str | None = None) -> None:
    rendered = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    atomic_write_text(path, rendered, task_id=task_id)


def atomic_replace(source: Path, target: Path, *, task_id: str | None = None) -> None:
    src = Path(source)
    dst = Path(target)
    require_write_access(dst, task_id=task_id)
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)
