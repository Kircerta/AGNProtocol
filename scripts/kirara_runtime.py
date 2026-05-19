#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = ROOT / "runtime"
OUTBOX_PATH = RUNTIME_DIR / "kirara_outbox.jsonl"
TASKS_PATH = RUNTIME_DIR / "kirara_tasks.json"
HEARTBEAT_STATE_PATH = RUNTIME_DIR / "kirara_heartbeat_state.json"

VALID_MESSAGE_KINDS = {"dialogue", "progress", "alert", "greeting"}

try:
    from memory_sync import append_memory_event
except Exception:  # pragma: no cover - memory sync is optional runtime dependency
    append_memory_event = None


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def load_json_or_default(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(default)
    if isinstance(payload, dict):
        return payload
    return dict(default)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    """Append a single JSON line with advisory locking and fsync."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def normalize_message_kind(kind: str) -> str:
    candidate = str(kind or "").strip().lower()
    if candidate in VALID_MESSAGE_KINDS:
        return candidate
    return "dialogue"


def enqueue_message(
    *,
    text: str,
    chat_id: str = "",
    task_id: str = "",
    correlation_id: str = "",
    message_kind: str = "dialogue",
    source: str = "kirara",
    outbox_path: Path = OUTBOX_PATH,
) -> dict[str, Any]:
    rendered = str(text or "").strip()
    if not rendered:
        raise ValueError("text is required")

    payload = {
        "message_id": f"kmsg-{uuid4().hex[:16]}",
        "created_at": utc_now_iso(),
        "source": str(source or "kirara").strip() or "kirara",
        "kind": normalize_message_kind(message_kind),
        "text": rendered,
        "chat_id": str(chat_id or "").strip(),
        "task_id": str(task_id or "").strip(),
        "correlation_id": str(correlation_id or "").strip(),
    }
    append_jsonl(outbox_path, payload)
    if append_memory_event is not None:
        try:
            append_memory_event(
                key=f"kirara_outbox:{payload['message_id']}",
                payload=payload,
                kind="kirara_outbox_message",
                source=str(source or "kirara"),
                task_id=payload["task_id"],
                correlation_id=payload["correlation_id"],
            )
        except Exception:
            pass
    return payload


def read_jsonl_from_offset(path: Path, offset: int) -> tuple[int, list[dict[str, Any]]]:
    if not path.exists():
        return 0, []

    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        size = path.stat().st_size
        safe_offset = max(0, min(offset, size))
        handle.seek(safe_offset)
        while True:
            line = handle.readline()
            if not line:
                break
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        next_offset = handle.tell()
    return next_offset, events


def load_task_registry(path: Path = TASKS_PATH) -> dict[str, Any]:
    payload = load_json_or_default(path, {"tasks": []})
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        payload["tasks"] = []
    return payload


def save_task_registry(payload: dict[str, Any], path: Path = TASKS_PATH) -> None:
    atomic_write_json(path, payload)
    if append_memory_event is not None:
        try:
            append_memory_event(
                key="kirara_tasks:registry",
                payload=payload,
                kind="kirara_tasks_snapshot",
                source="kirara_runtime",
            )
        except Exception:
            pass
