#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runner import append_audit
from kirara_runtime import enqueue_message


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Kirara explicit user-message tool. Only messages sent via this tool are user-visible in explicit mode."
    )
    parser.add_argument("--text", required=True)
    parser.add_argument("--chat-id", default="")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--correlation-id", default="")
    parser.add_argument("--kind", default="dialogue", choices=["dialogue", "progress", "alert", "greeting"])
    parser.add_argument("--source", default="kirara")
    args = parser.parse_args()

    try:
        payload = enqueue_message(
            text=args.text,
            chat_id=args.chat_id,
            task_id=args.task_id,
            correlation_id=args.correlation_id,
            message_kind=args.kind,
            source=args.source,
        )
    except Exception as exc:
        append_audit(
            action="kirara_message_enqueue_failed",
            task_id=args.task_id or None,
            route="/kirara/message_tool",
            status=500,
            correlation_id=args.correlation_id or None,
            error=type(exc).__name__,
        )
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True))
        return 1

    append_audit(
        action="kirara_message_enqueued",
        task_id=payload.get("task_id") or None,
        route="/kirara/message_tool",
        status=200,
        correlation_id=payload.get("correlation_id") or None,
        chat_id=payload.get("chat_id", ""),
        message_id=payload.get("message_id", ""),
        kind=payload.get("kind", "dialogue"),
    )
    print(json.dumps({"ok": True, **payload}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
