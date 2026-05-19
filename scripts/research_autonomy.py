#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from agn_api.ssot_store import SSOTStore
from event_sourcing import load_checkpoint, write_checkpoint
from agn_notify_runtime import enqueue_message
from network_runtime import effective_windows, load_autonomy_config
from pointer_protocol import read_ref_text, write_json_artifact
from research_flow import run_research_unit

RUNTIME_DIR = ROOT / "runtime"
STATE_PATH = RUNTIME_DIR / "research_autonomy_state.json"


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"windows": {}, "days": {}}
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"windows": {}, "days": {}}
    if isinstance(payload, dict):
        if not isinstance(payload.get("days"), dict):
            payload["days"] = {}
        return payload
    return {"windows": {}, "days": {}}


def _save_state(payload: dict[str, Any]) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


def _task_done(task_id: str) -> bool:
    checkpoint = load_checkpoint(task_id) or {}
    phase = str(checkpoint.get("research_phase", "")).strip().lower()
    state = str(checkpoint.get("state", "")).strip().upper()
    if phase == "done" or state == "DELIVERED":
        return True
    store = SSOTStore(ROOT / "ssot")
    task = store.get_task(task_id) or {}
    return str(task.get("decision", "")).strip() in {"approved", "rejected"} and bool(str(task.get("archive_ref", "")).strip())


def _launch(task_id: str, unit_date: str, executor_provider: str, reviewer_provider: str, chat_id: str) -> None:
    log_dir = ROOT / "reports"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"research_autonomy_{task_id.replace('/', '_')}.log"
    with log_path.open("a", encoding="utf-8") as handle:
        subprocess.Popen(
            [
                sys.executable,
                "scripts/research_flow.py",
                "--task-id",
                task_id,
                "--unit-date",
                unit_date,
                "--scenario",
                "daily",
                "--executor-provider",
                executor_provider,
                "--reviewer-provider",
                reviewer_provider,
                "--chat-id",
                chat_id,
                "--source",
                "autonomy_afternoon",
                "--max-steps",
                "32",
            ],
            cwd=str(ROOT),
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def _brief_payload(task_id: str) -> dict[str, Any]:
    checkpoint = load_checkpoint(task_id) or {}
    survey_ref = str(checkpoint.get("survey_ref", "")).strip()
    if not survey_ref.startswith("agn://"):
        return {"task_id": task_id, "candidates": []}
    survey = json.loads(read_ref_text(survey_ref, mode="all", max_bytes=512 * 1024))
    candidates: list[dict[str, Any]] = []
    for item in (survey.get("candidates", []) or [])[:3]:
        if not isinstance(item, dict):
            continue
        candidates.append(
            {
                "title": str(item.get("title", "")).strip(),
                "research_axis": str(item.get("axis", "")).strip(),
                "why_relevant": str(item.get("focus", "")).strip() or str(item.get("axis", "")).strip(),
                "why_worth_doing": str(item.get("survey_note", "")).strip(),
                "can_be_done_today": bool(item.get("data_ready", False) and item.get("baseline_clear", False) and item.get("fixed_budget", False)),
                "expected_learning_value": float(item.get("learning_value", item.get("score", 0.0)) or 0.0),
                "topic_id": str(item.get("topic_id", "")).strip(),
            }
        )
    return {"task_id": task_id, "candidates": candidates}


def _external_survey_snapshot() -> dict[str, Any]:
    token = str(os.getenv("BRAVE_API_KEY", "")).strip()
    if not token:
        return {"mode": "local_fallback", "results": []}

    queries = [
        "audio separation transformer attention signal processing research",
        "frequency response correction local global signal processing research",
    ]
    results: list[dict[str, str]] = []
    for query in queries:
        proc = subprocess.run(
            [
                sys.executable,
                "scripts/agn_sense.py",
                "web-search",
                "--query",
                query,
                "--count",
                "3",
            ],
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=30.0,
        )
        if proc.returncode != 0:
            continue
        try:
            payload = json.loads(proc.stdout.strip() or "{}")
        except Exception:
            continue
        for item in payload.get("results", []) or []:
            if not isinstance(item, dict):
                continue
            results.append(
                {
                    "query": query,
                    "title": str(item.get("title", "")).strip(),
                    "url": str(item.get("url", "")).strip(),
                }
            )
    return {"mode": "brave_web_search", "results": results[:6]}


def _send_daily_brief(*, task_id: str, unit_date: str, chat_id: str, deadline_window: str) -> str:
    payload = _brief_payload(task_id)
    payload["external_survey"] = _external_survey_snapshot()
    artifact = write_json_artifact(
        task_id=task_id,
        attempt=1,
        artifact_id="daily_brief",
        payload=payload,
        filename="daily_brief.json",
        source="research_autonomy",
    )
    lines = [
        "[AGN research] daily brief",
        f"task_id={task_id}",
        f"reply_deadline={unit_date} {deadline_window}",
        f"external_survey_mode={str((payload.get('external_survey') or {}).get('mode', 'local_fallback')).strip()}",
        "If no reply arrives before the deadline, autonomy mode will continue.",
    ]
    for idx, item in enumerate(payload.get("candidates", []), start=1):
        if not isinstance(item, dict):
            continue
        lines.extend(
            [
                f"{idx}. {str(item.get('title', '')).strip()}",
                f"   axis={str(item.get('research_axis', '')).strip()}",
                f"   why_relevant={str(item.get('why_relevant', '')).strip()}",
                f"   why_worth_doing={str(item.get('why_worth_doing', '')).strip()}",
                f"   can_be_done_today={bool(item.get('can_be_done_today', False))}",
                f"   expected_learning_value={float(item.get('expected_learning_value', 0.0) or 0.0):.2f}",
            ]
        )
    lines.append("Reply with /research start then send Research Question/Hypothesis, or use /research start minimal.")
    enqueue_message(
        text="\n".join(lines),
        chat_id=chat_id,
        task_id=task_id,
        correlation_id=task_id,
        message_kind="alert",
        source="research_autonomy",
    )
    return artifact.ref


def _run_once(*, windows: list[str], executor_provider: str, reviewer_provider: str, chat_id: str) -> int:
    now = datetime.now()
    today = now.date().isoformat()
    task_id = f"research-{today}"
    state = _load_state()
    window_state = state.setdefault("windows", {})
    day_state = state.setdefault("days", {}).setdefault(today, {})
    if not isinstance(window_state, dict):
        window_state = {}
        state["windows"] = window_state
    if not isinstance(day_state, dict):
        day_state = {}
        state.setdefault("days", {})[today] = day_state

    launched = 0
    morning_window = windows[0] if windows else "09:00"
    afternoon_window = windows[1] if len(windows) > 1 else morning_window
    morning_due = datetime.strptime(f"{today} {morning_window}", "%Y-%m-%d %H:%M")
    afternoon_due = datetime.strptime(f"{today} {afternoon_window}", "%Y-%m-%d %H:%M")
    morning_slot = f"{today}@{morning_window}"
    afternoon_slot = f"{today}@{afternoon_window}"

    if now >= morning_due and morning_slot not in window_state:
        if _task_done(task_id):
            window_state[morning_slot] = {"status": "skipped_done", "recorded_at": now.isoformat(), "task_id": task_id}
        else:
            summary = run_research_unit(
                task_id=task_id,
                unit_date=today,
                scenario="daily",
                max_steps=1,
                executor_provider=executor_provider,
                reviewer_provider=reviewer_provider,
                chat_id=chat_id,
                source="autonomy_morning",
                research_mode="autonomy",
                awaiting_admin_until=f"{today} {afternoon_window}",
            )
            brief_ref = _send_daily_brief(task_id=task_id, unit_date=today, chat_id=chat_id, deadline_window=afternoon_window)
            checkpoint = load_checkpoint(task_id) or {}
            if checkpoint:
                checkpoint["daily_brief_ref"] = brief_ref
                checkpoint["daily_brief_deadline"] = f"{today} {afternoon_window}"
                checkpoint["awaiting_admin_response"] = True
                write_checkpoint(task_id, checkpoint)
            store = SSOTStore(ROOT / "ssot")
            with store.locked_update(task_id) as task:
                if task is not None:
                    task["daily_brief_ref"] = brief_ref
                    task["awaiting_admin_until"] = f"{today} {afternoon_window}"
                    task["chat_id"] = chat_id
            day_state.update(
                {
                    "task_id": task_id,
                    "chat_id": chat_id,
                    "brief_ref": brief_ref,
                    "awaiting_admin_until": f"{today} {afternoon_window}",
                    "manual_override": bool(day_state.get("manual_override", False)),
                    "morning_summary": summary,
                }
            )
            window_state[morning_slot] = {"status": "brief_sent", "recorded_at": now.isoformat(), "task_id": task_id}

    if now >= afternoon_due and afternoon_slot not in window_state:
        if _task_done(task_id):
            window_state[afternoon_slot] = {"status": "skipped_done", "recorded_at": now.isoformat(), "task_id": task_id}
        elif bool(day_state.get("manual_override", False)):
            window_state[afternoon_slot] = {"status": "manual_override", "recorded_at": now.isoformat(), "task_id": task_id}
        else:
            _launch(task_id, today, executor_provider, reviewer_provider, chat_id)
            window_state[afternoon_slot] = {"status": "launched_autonomy", "recorded_at": now.isoformat(), "task_id": task_id}
            launched = 1

    _save_state(state)
    print(
        json.dumps(
            {
                "ok": True,
                "task_id": task_id,
                "today": today,
                "launched": launched,
                "windows": windows,
                "day_state": day_state,
            },
            ensure_ascii=True,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Minimal two-window scheduler for AGN daily research")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--windows", default="")
    parser.add_argument("--executor-provider", default=os.getenv("EXECUTOR_PROVIDER", "codex"))
    parser.add_argument("--reviewer-provider", default=os.getenv("REVIEWER_PROVIDER", "gemini"))
    parser.add_argument("--chat-id", default=os.getenv("AGN_TELEGRAM_ADMIN_CHAT_ID", ""))
    args = parser.parse_args()

    executor_provider = str(args.executor_provider or "codex").strip().lower() or "codex"
    reviewer_provider = str(args.reviewer_provider or "gemini").strip().lower() or "gemini"
    chat_id = str(args.chat_id or "").strip()

    if args.once:
        autonomy_config = load_autonomy_config()
        if str(args.windows or "").strip():
            windows = [str(item).strip() for item in str(args.windows).split(",") if str(item).strip()]
        else:
            windows = effective_windows(autonomy_config)
        if not bool(autonomy_config.get("auto_enabled", True)):
            print(json.dumps({"ok": True, "launched": 0, "windows": windows, "auto_enabled": False}, ensure_ascii=True))
            return 0
        return _run_once(
            windows=windows,
            executor_provider=executor_provider,
            reviewer_provider=reviewer_provider,
            chat_id=chat_id,
        )

    from agn.governance.bridge import global_emergency_stop_active, emit_agn1_audit

    while True:
        # ── AGN2.0 global emergency stop gate ──
        if global_emergency_stop_active():
            emit_agn1_audit(
                "emergency_stop_pausing",
                worker="research_autonomy",
                reason="agn2_global_emergency_stop_active",
            )
            time.sleep(max(5.0, float(args.interval_seconds)))
            continue
        # ── end AGN2.0 gate ──

        autonomy_config = load_autonomy_config()
        if str(args.windows or "").strip():
            windows = [str(item).strip() for item in str(args.windows).split(",") if str(item).strip()]
        else:
            windows = effective_windows(autonomy_config)
        if not bool(autonomy_config.get("auto_enabled", True)):
            time.sleep(max(5.0, float(args.interval_seconds)))
            continue
        _run_once(
            windows=windows,
            executor_provider=executor_provider,
            reviewer_provider=reviewer_provider,
            chat_id=chat_id,
        )
        time.sleep(max(5.0, float(args.interval_seconds)))


if __name__ == "__main__":
    raise SystemExit(main())
