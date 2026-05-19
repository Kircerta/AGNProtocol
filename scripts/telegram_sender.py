#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runner import append_audit
from agn_notify_runtime import read_jsonl_from_offset
try:
    from research_runtime import resolve_telegram_bot_token
except ImportError:  # pragma: no cover - package import fallback
    from scripts.research_runtime import resolve_telegram_bot_token

RUNTIME_DIR = ROOT / "runtime"
STATE_PATH = RUNTIME_DIR / "telegram_sent.json"
CORR_MAP_PATH = RUNTIME_DIR / "telegram_corr_map.json"
AUDIT_PATH = ROOT / "audit" / "events.jsonl"
OUTBOX_PATH = RUNTIME_DIR / "agn_telegram_outbox.jsonl"
SSOT_DIR = ROOT / "ssot"
RESULTS_DIR = ROOT / "results"
VERDICTS_DIR = ROOT / "verdicts"
VALID_NOTIFY_MODES = {"explicit", "final", "verbose"}


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
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def load_corr_map() -> dict[str, dict[str, str]]:
    payload = load_json_or_default(CORR_MAP_PATH, {"map": {}})
    mapping = payload.get("map")
    if isinstance(mapping, dict):
        cleaned: dict[str, dict[str, str]] = {}
        for key, value in mapping.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            chat_id = str(value.get("chat_id", "")).strip()
            task_id = str(value.get("task_id", "")).strip()
            if not chat_id:
                continue
            cleaned[key] = {"chat_id": chat_id, "task_id": task_id}
        return cleaned
    return {}


def find_chat_id_from_ssot(task_id: str, correlation_id: str) -> str:
    if task_id:
        task_path = SSOT_DIR / f"{task_id}.json"
        if task_path.exists():
            try:
                payload = json.loads(task_path.read_text(encoding="utf-8"))
                chat_id = str(payload.get("chat_id", "")).strip()
                if chat_id:
                    return chat_id
            except Exception:
                pass

    if correlation_id and SSOT_DIR.exists():
        try:
            from agn_api.ssot_store import SSOTStore
            store = SSOTStore(SSOT_DIR)
            task = store.get_task_by_correlation(correlation_id)
            if task is not None:
                chat_id = str(task.get("chat_id", "")).strip()
                if chat_id:
                    return chat_id
        except Exception:
            # Fallback: linear scan if SSOTStore import fails.
            for path in SSOT_DIR.glob("*.json"):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if str(payload.get("correlation_id", "")).strip() != correlation_id:
                    continue
                chat_id = str(payload.get("chat_id", "")).strip()
                if chat_id:
                    return chat_id
    return ""


def telegram_request(token: str, method: str, payload: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    with httpx.Client(timeout=timeout_sec) as client:
        response = client.post(url, json=payload)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or not data.get("ok", False):
        raise RuntimeError("telegram api returned non-ok")
    return data


def send_message(token: str | None, chat_id: str, text: str, dry_run: bool, timeout_sec: float) -> None:
    if dry_run or not token:
        print(f"[telegram_sender] would_send chat_id={chat_id}: {text}")
        return
    telegram_request(
        token,
        "sendMessage",
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout_sec,
    )


def _normalize_telegram_text(text: str) -> str:
    rendered = str(text or "").strip()
    if len(rendered) <= 4096:
        return rendered
    return rendered[:4060] + "\n... (truncated)"


def is_interesting_event(event: dict[str, Any], notify_mode: str) -> bool:
    if notify_mode == "explicit":
        return False

    route = str(event.get("route", ""))
    action = str(event.get("action", ""))
    if route == "/agn/coordinator" and action == "hallucination_lock_triggered":
        return notify_mode == "final"
    if route == "/dispatch/acks" and action == "executor_ack_written":
        return notify_mode == "verbose"
    if route == "/agn/executor" and action in {"executor_processed", "executor_failed"}:
        return notify_mode == "verbose"
    if route == "/agn/reviewer" and action in {"reviewer_processed", "reviewer_failed"}:
        return True
    return False


def load_result_summary(task_id: str, attempt: int) -> tuple[str, str]:
    result_path = RESULTS_DIR / f"{task_id}.{attempt}.json"
    if not result_path.exists():
        return "", ""
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except Exception:
        return str(result_path.relative_to(ROOT)), ""

    commit_hash = str(payload.get("commit_hash", "")).strip()
    no_change_reason = str(payload.get("no_change_reason", "")).strip()
    summary = ""
    if commit_hash:
        summary = f"commit_hash={commit_hash}"
    elif no_change_reason:
        summary = f"no_change_reason={no_change_reason}"
    return str(result_path.relative_to(ROOT)), summary


def load_verdict_summary(task_id: str, attempt: int) -> tuple[str, str, str]:
    verdict_path = VERDICTS_DIR / f"{task_id}.{attempt}.json"
    if not verdict_path.exists():
        return "", "", ""
    try:
        payload = json.loads(verdict_path.read_text(encoding="utf-8"))
    except Exception:
        return str(verdict_path.relative_to(ROOT)), "", ""

    decision = str(payload.get("decision", "")).strip()
    issues = payload.get("issues", [])
    issue_titles: list[str] = []
    if isinstance(issues, list):
        for issue in issues[:2]:
            if isinstance(issue, dict):
                title = str(issue.get("title", "")).strip()
                if title:
                    issue_titles.append(title)
    issues_text = "; ".join(issue_titles)
    return str(verdict_path.relative_to(ROOT)), decision, issues_text


def build_stage_message(event: dict[str, Any], notify_mode: str) -> tuple[str, str]:
    route = str(event.get("route", ""))
    action = str(event.get("action", ""))
    task_id = str(event.get("task_id", "")).strip()
    corr = str(event.get("correlation_id", "")).strip()
    attempt = int(event.get("attempt", 0) or 0)

    if route == "/agn/coordinator" and action == "hallucination_lock_triggered":
        stage = "lock"
        lines = [
            "[AGN] FINAL halted",
            f"task_id={task_id}",
            f"correlation_id={corr}",
        ]
        lock_reason = str(event.get("lock_reason", "")).strip()
        qa_retry_count = int(event.get("qa_retry_count", 0) or 0)
        if lock_reason:
            lines.append(f"lock_reason={lock_reason}")
        lines.append(f"qa_retry_count={qa_retry_count}")
        return stage, "\n".join(lines)

    if notify_mode == "verbose" and route == "/dispatch/acks" and action == "executor_ack_written":
        stage = "ack"
        text = (
            f"[AGN] ACK\n"
            f"task_id={task_id}\n"
            f"attempt={attempt}\n"
            f"correlation_id={corr}"
        )
        return stage, text

    if notify_mode == "verbose" and route == "/agn/executor" and action in {"executor_processed", "executor_failed"}:
        stage = "exec"
        result_rel, summary = load_result_summary(task_id, attempt)
        status = "done" if action == "executor_processed" else "failed"
        lines = [
            f"[AGN] EXEC {status}",
            f"task_id={task_id}",
            f"attempt={attempt}",
        ]
        if summary:
            lines.append(summary)
        if result_rel:
            lines.append(f"result={result_rel}")
        return stage, "\n".join(lines)

    if route == "/agn/reviewer" and action in {"reviewer_processed", "reviewer_failed"}:
        stage = "review"
        verdict_rel, decision, issues_text = load_verdict_summary(task_id, attempt)
        status = decision or ("failed" if action == "reviewer_failed" else "unknown")
        lines = [
            f"[AGN] {'FINAL' if notify_mode == 'final' else 'REVIEW'} {status}",
            f"task_id={task_id}",
            f"attempt={attempt}",
        ]
        if verdict_rel:
            lines.append(f"verdict={verdict_rel}")
        if issues_text:
            lines.append(f"issues={issues_text}")
        return stage, "\n".join(lines)

    return "", ""


def resolve_chat_id(event: dict[str, Any], corr_map: dict[str, dict[str, str]]) -> str:
    chat_id = str(event.get("chat_id", "")).strip()
    if chat_id:
        return chat_id

    corr = str(event.get("correlation_id", "")).strip()
    if corr and corr in corr_map:
        mapped_chat = str(corr_map[corr].get("chat_id", "")).strip()
        if mapped_chat:
            return mapped_chat

    task_id = str(event.get("task_id", "")).strip()
    return find_chat_id_from_ssot(task_id, corr)


def process_explicit_entry(
    *,
    entry: dict[str, Any],
    sent_keys: set[str],
    token: str | None,
    dry_run: bool,
    timeout_sec: float,
) -> None:
    text = _normalize_telegram_text(str(entry.get("text", "")).strip())
    if not text:
        return

    message_id = str(entry.get("message_id", "")).strip() or f"kmsg-missing-{uuid4().hex[:12]}"
    dedup_key = f"explicit:{message_id}"
    if dedup_key in sent_keys:
        return

    corr_map = load_corr_map()
    chat_id = resolve_chat_id(entry, corr_map)
    task_id = str(entry.get("task_id", "")).strip() or None
    corr = str(entry.get("correlation_id", "")).strip() or None
    stage = str(entry.get("kind", "dialogue")).strip().lower() or "dialogue"

    if not chat_id:
        append_audit(
            action="telegram_send_skipped",
            task_id=task_id,
            route="/telegram/sender",
            status=404,
            correlation_id=corr,
            stage=f"explicit:{stage}",
            reason="chat_id_not_found",
            message_id=message_id,
        )
        sent_keys.add(dedup_key)
        return

    try:
        send_message(token, chat_id, text, dry_run, timeout_sec)
    except Exception as exc:
        append_audit(
            action="telegram_send_failed",
            task_id=task_id,
            route="/telegram/sender",
            status=500,
            correlation_id=corr,
            stage=f"explicit:{stage}",
            error=type(exc).__name__,
            message_id=message_id,
        )
        return

    append_audit(
        action="telegram_sent",
        task_id=task_id,
        route="/telegram/sender",
        status=200,
        correlation_id=corr,
        stage=f"explicit:{stage}",
        message_id=message_id,
    )
    sent_keys.add(dedup_key)


def process_event(
    *,
    event: dict[str, Any],
    sent_keys: set[str],
    token: str | None,
    dry_run: bool,
    timeout_sec: float,
    notify_mode: str,
) -> None:
    if not is_interesting_event(event, notify_mode):
        return

    task_id = str(event.get("task_id", "")).strip()
    attempt = int(event.get("attempt", 0) or 0)
    corr_map = load_corr_map()
    chat_id = resolve_chat_id(event, corr_map)
    task_id_or_none = task_id or None
    corr = str(event.get("correlation_id", "")).strip() or None

    stage, message = build_stage_message(event, notify_mode)
    if not stage or not message:
        return

    dedup_key = f"{task_id}:{attempt}:{stage}"
    if dedup_key in sent_keys:
        return

    if not chat_id:
        append_audit(
            action="telegram_send_skipped",
            task_id=task_id_or_none,
            route="/telegram/sender",
            status=404,
            correlation_id=corr,
            stage=stage,
            reason="chat_id_not_found",
        )
        sent_keys.add(dedup_key)
        return

    try:
        send_message(token, chat_id, message, dry_run, timeout_sec)
    except Exception as exc:
        append_audit(
            action="telegram_send_failed",
            task_id=task_id_or_none,
            route="/telegram/sender",
            status=500,
            correlation_id=corr,
            stage=stage,
            error=type(exc).__name__,
        )
        return

    append_audit(
        action="telegram_sent",
        task_id=task_id_or_none,
        route="/telegram/sender",
        status=200,
        correlation_id=corr,
        stage=stage,
    )
    sent_keys.add(dedup_key)


def run(args: argparse.Namespace) -> int:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    state = load_json_or_default(STATE_PATH, {"offset": 0, "outbox_offset": 0, "sent": []})
    offset = int(state.get("offset", 0) or 0)
    outbox_offset = int(state.get("outbox_offset", 0) or 0)
    sent_raw = state.get("sent", [])
    sent_keys = {item for item in sent_raw if isinstance(item, str)}

    token = resolve_telegram_bot_token()
    notify_mode = str(args.notify_mode or os.getenv("TELEGRAM_NOTIFY_MODE", "explicit")).strip().lower()
    if notify_mode not in VALID_NOTIFY_MODES:
        notify_mode = "explicit"
    if not token and not args.dry_run:
        print("TELEGRAM_BOT_TOKEN env set: false")
        return 1
    print(f"TELEGRAM_BOT_TOKEN env set: {'true' if bool(token) else 'false'}")
    print(f"TELEGRAM_NOTIFY_MODE={notify_mode}")

    append_audit(
        action="telegram_sender_started",
        task_id=None,
        route="/telegram/sender",
        status=200,
        once=args.once,
        dry_run=args.dry_run,
        notify_mode=notify_mode,
    )

    try:
        while True:
            if notify_mode == "explicit":
                next_offset, entries = read_jsonl_from_offset(OUTBOX_PATH, outbox_offset)
                outbox_offset = next_offset
                for entry in entries:
                    process_explicit_entry(
                        entry=entry,
                        sent_keys=sent_keys,
                        token=token,
                        dry_run=args.dry_run,
                        timeout_sec=args.http_timeout_seconds,
                    )
            else:
                if not AUDIT_PATH.exists():
                    if args.once:
                        break
                    time.sleep(max(0.1, args.sleep_seconds))
                    continue

                file_size = AUDIT_PATH.stat().st_size
                if offset > file_size:
                    offset = 0

                with AUDIT_PATH.open("r", encoding="utf-8") as handle:
                    handle.seek(offset)
                    while True:
                        line = handle.readline()
                        if not line:
                            break
                        offset = handle.tell()
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(event, dict):
                            continue
                        process_event(
                            event=event,
                            sent_keys=sent_keys,
                            token=token,
                            dry_run=args.dry_run,
                            timeout_sec=args.http_timeout_seconds,
                            notify_mode=notify_mode,
                        )

            state["offset"] = offset
            state["outbox_offset"] = outbox_offset
            # Keep bounded size to avoid unbounded file growth.
            state["sent"] = sorted(list(sent_keys))[-5000:]
            state["updated_at"] = utc_now_iso()
            atomic_write_json(STATE_PATH, state)

            if args.once:
                break
            time.sleep(max(0.1, args.sleep_seconds))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        append_audit(
            action="telegram_sender_crashed",
            task_id=None,
            route="/telegram/sender",
            status=500,
            error=type(exc).__name__,
        )
        return 1
    finally:
        append_audit(
            action="telegram_sender_stopped",
            task_id=None,
            route="/telegram/sender",
            status=200,
        )

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram sender for AGN audit events")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sleep-seconds", type=float, default=1.0)
    parser.add_argument("--http-timeout-seconds", type=float, default=20.0)
    parser.add_argument("--notify-mode", choices=sorted(VALID_NOTIFY_MODES), default=os.getenv("TELEGRAM_NOTIFY_MODE", "explicit"))
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
