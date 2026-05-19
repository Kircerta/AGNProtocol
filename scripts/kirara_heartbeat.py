#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runner import PATHS, append_audit
from kirara_runtime import (
    HEARTBEAT_STATE_PATH,
    OUTBOX_PATH,
    TASKS_PATH,
    atomic_write_json,
    enqueue_message,
    load_json_or_default,
)


def _parse_iso(ts: str) -> datetime | None:
    raw = str(ts or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _cooldown_elapsed(last_iso: str, now: datetime, cooldown_seconds: int) -> bool:
    last = _parse_iso(last_iso)
    if last is None:
        return True
    return (now - last).total_seconds() >= max(1, cooldown_seconds)


def _state_bucket(state: dict[str, Any], bucket: str) -> dict[str, str]:
    current = state.get(bucket)
    if isinstance(current, dict):
        cleaned: dict[str, str] = {}
        for key, value in current.items():
            if isinstance(key, str) and isinstance(value, str):
                cleaned[key] = value
        state[bucket] = cleaned
        return cleaned
    replacement: dict[str, str] = {}
    state[bucket] = replacement
    return replacement


def _render_due_message(task: dict[str, Any], minutes_left: int) -> str:
    task_id = str(task.get("task_id") or task.get("id") or "unknown").strip()
    title = str(task.get("title") or task.get("request_text") or "task").strip()
    if minutes_left < 0:
        return f"[Kirara] Task overdue. task_id={task_id}. {title}"
    return f"[Kirara] Deadline approaching ({minutes_left}m). task_id={task_id}. {title}"


def _safe_task_id(task: dict[str, Any]) -> str:
    task_id = str(task.get("task_id") or task.get("id") or "").strip()
    if task_id:
        return task_id
    title = str(task.get("title") or task.get("request_text") or "task").strip()
    if title:
        return title[:48]
    return "task"


def _collect_stale_ssot_alerts(
    *,
    now: datetime,
    stale_timeout_seconds: int,
    stale_alerts: dict[str, str],
    cooldown_seconds: int,
    outbox_path: Path,
) -> int:
    sent = 0
    if not PATHS.ssot_dir.exists():
        return sent

    threshold = max(60, stale_timeout_seconds)
    for task_file in PATHS.ssot_dir.glob("*.json"):
        task_id = task_file.stem
        if task_id.startswith("telegram-"):
            continue

        stale_for = (now - datetime.fromtimestamp(task_file.stat().st_mtime, tz=timezone.utc)).total_seconds()
        if stale_for <= threshold:
            continue

        if not _cooldown_elapsed(stale_alerts.get(task_id, ""), now, cooldown_seconds):
            continue

        message = (
            "[Kirara] AGN heartbeat timeout. "
            f"task_id={task_id}, stale_seconds={int(stale_for)}"
        )
        enqueue_message(
            text=message,
            task_id=task_id,
            message_kind="alert",
            source="heartbeat",
            outbox_path=outbox_path,
        )
        stale_alerts[task_id] = now.isoformat()
        append_audit(
            action="heartbeat_timeout",
            task_id=task_id,
            route="/kirara/heartbeat",
            status=200,
            stale_seconds=int(stale_for),
        )
        sent += 1
    return sent


def run_tick(
    *,
    tasks_path: Path = TASKS_PATH,
    state_path: Path = HEARTBEAT_STATE_PATH,
    outbox_path: Path = OUTBOX_PATH,
    due_soon_minutes: int = 60,
    stale_timeout_seconds: int = 1800,
    cooldown_seconds: int = 3600,
    allow_greeting: bool = False,
    now: datetime | None = None,
) -> dict[str, int | bool]:
    current = now or datetime.now(tz=timezone.utc)

    task_payload = load_json_or_default(tasks_path, {"tasks": []})
    tasks = task_payload.get("tasks")
    if not isinstance(tasks, list):
        tasks = []

    # R2-7: advisory lock on state file to prevent concurrent heartbeats from
    # losing each other's updates (read-modify-write race).
    state_lock_path = state_path.parent / f".{state_path.name}.lock"
    state_lock_path.parent.mkdir(parents=True, exist_ok=True)
    _hb_lock_fd = os.open(str(state_lock_path), os.O_CREAT | os.O_RDWR)
    fcntl.flock(_hb_lock_fd, fcntl.LOCK_EX)
    try:
        state = load_json_or_default(
            state_path,
            {
                "updated_at": "",
                "task_due_alerts": {},
                "task_overdue_alerts": {},
                "stale_alerts": {},
                "last_greeting_at": "",
            },
        )
        due_alerts = _state_bucket(state, "task_due_alerts")
        overdue_alerts = _state_bucket(state, "task_overdue_alerts")
        stale_alerts = _state_bucket(state, "stale_alerts")

        reminders_sent = 0
        overdue_sent = 0
        stale_sent = 0

        for raw_task in tasks:
            if not isinstance(raw_task, dict):
                continue
            status = str(raw_task.get("status") or "active").strip().lower()
            if status in {"done", "completed", "cancelled", "closed"}:
                continue

            due_at = _parse_iso(str(raw_task.get("due_at") or ""))
            if due_at is None:
                continue

            task_id = _safe_task_id(raw_task)
            minutes_left = int((due_at - current).total_seconds() // 60)
            correlation_id = str(raw_task.get("correlation_id") or "").strip()
            chat_id = str(raw_task.get("chat_id") or "").strip()

            if minutes_left < 0:
                if not _cooldown_elapsed(overdue_alerts.get(task_id, ""), current, cooldown_seconds):
                    continue
                enqueue_message(
                    text=_render_due_message(raw_task, minutes_left),
                    chat_id=chat_id,
                    task_id=task_id,
                    correlation_id=correlation_id,
                    message_kind="alert",
                    source="heartbeat",
                    outbox_path=outbox_path,
                )
                overdue_alerts[task_id] = current.isoformat()
                overdue_sent += 1
                continue

            threshold = int(raw_task.get("notify_before_minutes") or due_soon_minutes)
            if minutes_left > max(1, threshold):
                continue

            if not _cooldown_elapsed(due_alerts.get(task_id, ""), current, cooldown_seconds):
                continue

            enqueue_message(
                text=_render_due_message(raw_task, minutes_left),
                chat_id=chat_id,
                task_id=task_id,
                correlation_id=correlation_id,
                message_kind="progress",
                source="heartbeat",
                outbox_path=outbox_path,
            )
            due_alerts[task_id] = current.isoformat()
            reminders_sent += 1

        stale_sent += _collect_stale_ssot_alerts(
            now=current,
            stale_timeout_seconds=stale_timeout_seconds,
            stale_alerts=stale_alerts,
            cooldown_seconds=max(cooldown_seconds, 3600),
            outbox_path=outbox_path,
        )

        greeting_sent = False
        if allow_greeting and reminders_sent == 0 and overdue_sent == 0 and stale_sent == 0:
            last_greeting = _parse_iso(str(state.get("last_greeting_at") or ""))
            if last_greeting is None or (current - last_greeting) >= timedelta(hours=24):
                enqueue_message(
                    text="[Kirara] Hope your day is going smoothly.",
                    message_kind="greeting",
                    source="heartbeat",
                    outbox_path=outbox_path,
                )
                state["last_greeting_at"] = current.isoformat()
                greeting_sent = True

        state["updated_at"] = current.isoformat()
        atomic_write_json(state_path, state)
    finally:
        try:
            fcntl.flock(_hb_lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(_hb_lock_fd)

    silence = (reminders_sent + overdue_sent + stale_sent + int(greeting_sent)) == 0
    append_audit(
        action="heartbeat_silence" if silence else "heartbeat_message_enqueued",
        task_id=None,
        route="/kirara/heartbeat",
        status=200,
        reminders_sent=reminders_sent,
        overdue_sent=overdue_sent,
        stale_sent=stale_sent,
        greeting_sent=greeting_sent,
    )
    return {
        "silence": silence,
        "reminders_sent": reminders_sent,
        "overdue_sent": overdue_sent,
        "stale_sent": stale_sent,
        "greeting_sent": int(greeting_sent),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Kirara silent heartbeat loop")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=float(os.getenv("KIRARA_HEARTBEAT_INTERVAL_SECONDS", "1800")))
    parser.add_argument("--due-soon-minutes", type=int, default=int(os.getenv("KIRARA_DUE_SOON_MINUTES", "60")))
    parser.add_argument("--stale-timeout-seconds", type=int, default=int(os.getenv("AGN_HEARTBEAT_TIMEOUT_SECONDS", "1800")))
    parser.add_argument("--cooldown-seconds", type=int, default=int(os.getenv("KIRARA_HEARTBEAT_COOLDOWN_SECONDS", "3600")))
    parser.add_argument("--allow-greeting", action="store_true")
    args = parser.parse_args()

    try:
        while True:
            summary = run_tick(
                due_soon_minutes=max(1, args.due_soon_minutes),
                stale_timeout_seconds=max(60, args.stale_timeout_seconds),
                cooldown_seconds=max(60, args.cooldown_seconds),
                allow_greeting=bool(args.allow_greeting),
            )
            print(json.dumps(summary, ensure_ascii=True))
            if args.once:
                break
            time.sleep(max(1.0, args.interval_seconds))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
