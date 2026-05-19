#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kirara_runtime import load_task_registry, save_task_registry
from memory_sync import append_memory_event


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _parse_due_at(raw: str, due_in_minutes: int) -> str:
    text = str(raw or "").strip()
    if text:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    dt = datetime.now(tz=timezone.utc) + timedelta(minutes=max(1, due_in_minutes))
    return dt.isoformat()


def _add_task(args: argparse.Namespace) -> dict[str, Any]:
    payload = load_task_registry()
    tasks = payload.setdefault("tasks", [])
    if not isinstance(tasks, list):
        tasks = []
        payload["tasks"] = tasks

    task_id = str(args.task_id).strip()
    task = {
        "task_id": task_id,
        "title": str(args.title or args.task_id).strip(),
        "status": "active",
        "due_at": _parse_due_at(str(args.due_at or ""), int(args.due_in_minutes)),
        "chat_id": str(args.chat_id or "").strip(),
        "correlation_id": str(args.correlation_id or "").strip(),
        "notify_before_minutes": int(args.notify_before_minutes),
        "created_at": _utc_now_iso(),
    }

    replaced = False
    for idx, existing in enumerate(tasks):
        if isinstance(existing, dict) and str(existing.get("task_id", "")).strip() == task_id:
            tasks[idx] = task
            replaced = True
            break
    if not replaced:
        tasks.append(task)

    payload["updated_at"] = _utc_now_iso()
    save_task_registry(payload)
    try:
        append_memory_event(
            key=f"kirara_task:{task_id}",
            payload=task,
            kind="kirara_task_upsert",
            source="kirara_tasks",
            task_id=task_id,
            correlation_id=str(task.get("correlation_id", "")),
        )
    except Exception:
        pass
    return {"ok": True, "task": task, "replaced": replaced}


def _set_status(args: argparse.Namespace, status: str) -> dict[str, Any]:
    payload = load_task_registry()
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        return {"ok": False, "error": "tasks registry corrupted"}

    task_id = str(args.task_id).strip()
    for item in tasks:
        if not isinstance(item, dict):
            continue
        if str(item.get("task_id", "")).strip() != task_id:
            continue
        item["status"] = status
        item["updated_at"] = _utc_now_iso()
        payload["updated_at"] = _utc_now_iso()
        save_task_registry(payload)
        try:
            append_memory_event(
                key=f"kirara_task:{task_id}",
                payload=item,
                kind="kirara_task_status",
                source="kirara_tasks",
                task_id=task_id,
                correlation_id=str(item.get("correlation_id", "")),
            )
        except Exception:
            pass
        return {"ok": True, "task": item}
    return {"ok": False, "error": "task_id not found"}


def _list_tasks() -> dict[str, Any]:
    payload = load_task_registry()
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        tasks = []
    return {"ok": True, "count": len(tasks), "tasks": tasks}


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage runtime/kirara_tasks.json")
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list")

    add_parser = sub.add_parser("add")
    add_parser.add_argument("--task-id", required=True)
    add_parser.add_argument("--title", default="")
    add_parser.add_argument("--due-at", default="")
    add_parser.add_argument("--due-in-minutes", type=int, default=60)
    add_parser.add_argument("--notify-before-minutes", type=int, default=60)
    add_parser.add_argument("--chat-id", default="")
    add_parser.add_argument("--correlation-id", default="")

    done_parser = sub.add_parser("done")
    done_parser.add_argument("--task-id", required=True)

    cancel_parser = sub.add_parser("cancel")
    cancel_parser.add_argument("--task-id", required=True)

    args = parser.parse_args()

    if args.command == "list":
        result = _list_tasks()
    elif args.command == "add":
        result = _add_task(args)
    elif args.command == "done":
        result = _set_status(args, "done")
    else:
        result = _set_status(args, "cancelled")

    print(json.dumps(result, ensure_ascii=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
