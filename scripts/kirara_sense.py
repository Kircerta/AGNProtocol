#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runner import append_audit

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _print_json(payload: dict[str, Any]) -> int:
    print(json.dumps(payload, ensure_ascii=True))
    return 0 if payload.get("ok") else 1


def _run_web_search(*, query: str, count: int, key_env: str, timeout_sec: float) -> int:
    token = str(os.getenv(key_env, "")).strip()
    if not token:
        return _print_json({"ok": False, "error": f"missing_env:{key_env}"})

    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": token,
    }
    params = {"q": query, "count": max(1, min(10, count))}

    try:
        with httpx.Client(timeout=timeout_sec) as client:
            response = client.get(BRAVE_ENDPOINT, headers=headers, params=params)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        append_audit(
            action="kirara_web_search_failed",
            task_id=None,
            route="/kirara/sense",
            status=500,
            error=type(exc).__name__,
        )
        return _print_json({"ok": False, "error": f"web_search_failed:{type(exc).__name__}"})

    results: list[dict[str, str]] = []
    web = payload.get("web") if isinstance(payload, dict) else None
    items = web.get("results", []) if isinstance(web, dict) else []
    if isinstance(items, list):
        for row in items[: max(1, min(10, count))]:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            url = str(row.get("url") or "").strip()
            desc = str(row.get("description") or "").strip()
            if not (title or url):
                continue
            results.append({"title": title, "url": url, "description": desc})

    append_audit(
        action="kirara_web_search_ok",
        task_id=None,
        route="/kirara/sense",
        status=200,
        result_count=len(results),
    )
    return _print_json({"ok": True, "query": query, "result_count": len(results), "results": results})


def _run_osascript(script: str, timeout_sec: float) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["osascript", "-e", script],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=timeout_sec,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _parse_tsv_lines(raw: str, keys: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not raw:
        return rows

    for line in raw.splitlines():
        parts = [part.strip() for part in line.split("\t")]
        if len(parts) < len(keys):
            continue
        row = {keys[idx]: parts[idx] for idx in range(len(keys))}
        rows.append(row)
    return rows


def _mail_unread_script(limit: int) -> str:
    safe_limit = max(1, min(50, limit))
    return f'''
set rowSep to linefeed
set colSep to (ASCII character 9)
set outputLines to {{}}
tell application "Mail"
  set unreadMessages to (messages of inbox whose read status is false)
  set idx to 0
  repeat with m in unreadMessages
    set idx to idx + 1
    if idx > {safe_limit} then exit repeat
    set senderText to (sender of m as text)
    set subjectText to (subject of m as text)
    set dateText to (date received of m as text)
    set rowText to senderText & colSep & subjectText & colSep & dateText
    set end of outputLines to rowText
  end repeat
end tell
set AppleScript's text item delimiters to rowSep
set rendered to outputLines as text
set AppleScript's text item delimiters to ""
return rendered
'''


def _calendar_upcoming_script(minutes_ahead: int, limit: int) -> str:
    safe_minutes = max(1, min(7 * 24 * 60, minutes_ahead))
    safe_limit = max(1, min(50, limit))
    return f'''
set rowSep to linefeed
set colSep to (ASCII character 9)
set outputLines to {{}}
set maxRows to {safe_limit}
set rowCount to 0

tell application "Calendar"
  set nowDate to current date
  set endDate to nowDate + ({safe_minutes} * minutes)
  repeat with c in calendars
    set evs to (every event of c whose start date is greater than or equal to nowDate and start date is less than or equal to endDate)
    repeat with e in evs
      set rowCount to rowCount + 1
      if rowCount > maxRows then exit repeat
      set calName to (name of c as text)
      set titleText to (summary of e as text)
      set startText to (start date of e as text)
      set rowText to calName & colSep & titleText & colSep & startText
      set end of outputLines to rowText
    end repeat
    if rowCount > maxRows then exit repeat
  end repeat
end tell
set AppleScript's text item delimiters to rowSep
set rendered to outputLines as text
set AppleScript's text item delimiters to ""
return rendered
'''


def _run_mail_unread(*, limit: int, timeout_sec: float) -> int:
    rc, stdout, stderr = _run_osascript(_mail_unread_script(limit), timeout_sec)
    if rc != 0:
        append_audit(
            action="kirara_mail_unread_failed",
            task_id=None,
            route="/kirara/sense",
            status=500,
            error="osascript_failed",
        )
        return _print_json(
            {
                "ok": False,
                "error": "mail_access_failed",
                "hint": "Grant Terminal/OpenClaw Automation access to Mail in macOS Settings.",
                "stderr": stderr,
            }
        )

    messages = _parse_tsv_lines(stdout, ["sender", "subject", "received_at"])
    append_audit(
        action="kirara_mail_unread_ok",
        task_id=None,
        route="/kirara/sense",
        status=200,
        result_count=len(messages),
    )
    return _print_json({"ok": True, "count": len(messages), "messages": messages})


def _run_calendar_upcoming(*, minutes: int, limit: int, timeout_sec: float) -> int:
    rc, stdout, stderr = _run_osascript(_calendar_upcoming_script(minutes, limit), timeout_sec)
    if rc != 0:
        append_audit(
            action="kirara_calendar_failed",
            task_id=None,
            route="/kirara/sense",
            status=500,
            error="osascript_failed",
        )
        return _print_json(
            {
                "ok": False,
                "error": "calendar_access_failed",
                "hint": "Grant Terminal/OpenClaw Automation access to Calendar in macOS Settings.",
                "stderr": stderr,
            }
        )

    events = _parse_tsv_lines(stdout, ["calendar", "title", "start_at"])
    append_audit(
        action="kirara_calendar_ok",
        task_id=None,
        route="/kirara/sense",
        status=200,
        result_count=len(events),
    )
    return _print_json(
        {
            "ok": True,
            "window_minutes": max(1, min(7 * 24 * 60, minutes)),
            "count": len(events),
            "events": events,
            "generated_at": _utc_now_iso(),
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Kirara environment sensing tools (web, mail, calendar)")
    sub = parser.add_subparsers(dest="command", required=True)

    web_parser = sub.add_parser("web-search")
    web_parser.add_argument("--query", required=True)
    web_parser.add_argument("--count", type=int, default=5)
    web_parser.add_argument("--api-key-env", default="BRAVE_API_KEY")
    web_parser.add_argument("--timeout-seconds", type=float, default=15.0)

    mail_parser = sub.add_parser("mail-unread")
    mail_parser.add_argument("--limit", type=int, default=10)
    mail_parser.add_argument("--timeout-seconds", type=float, default=15.0)

    cal_parser = sub.add_parser("calendar-upcoming")
    cal_parser.add_argument("--minutes", type=int, default=12 * 60)
    cal_parser.add_argument("--limit", type=int, default=10)
    cal_parser.add_argument("--timeout-seconds", type=float, default=15.0)

    args = parser.parse_args()

    if args.command == "web-search":
        return _run_web_search(
            query=str(args.query).strip(),
            count=int(args.count),
            key_env=str(args.api_key_env).strip() or "BRAVE_API_KEY",
            timeout_sec=max(1.0, float(args.timeout_seconds)),
        )
    if args.command == "mail-unread":
        return _run_mail_unread(limit=int(args.limit), timeout_sec=max(1.0, float(args.timeout_seconds)))
    return _run_calendar_upcoming(
        minutes=int(args.minutes),
        limit=int(args.limit),
        timeout_sec=max(1.0, float(args.timeout_seconds)),
    )


if __name__ == "__main__":
    raise SystemExit(main())
