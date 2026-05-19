#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import os
import sys
from uuid import uuid4

import httpx


DEFAULT_TELEGRAM_BASE = "https://api.telegram.org"
DEFAULT_BACKEND_BASE = "http://127.0.0.1:8000"
STATE_PATH = Path("./reports/telegram_polling_state.json")



def _load_state() -> dict[str, int]:
    if not STATE_PATH.exists():
        return {"last_update_id": 0}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"last_update_id": 0}
    if not isinstance(data, dict):
        return {"last_update_id": 0}
    return {"last_update_id": int(data.get("last_update_id", 0))}



def _save_state(last_update_id: int) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"last_update_id": int(last_update_id)}
    STATE_PATH.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")



def _normalize_update(update: dict) -> dict | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None

    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None

    chat_id = chat.get("id")
    message_id = message.get("message_id")
    text = message.get("text")
    if chat_id is None or message_id is None or text is None:
        return None

    msg_date = message.get("date")
    if isinstance(msg_date, (int, float)):
        created_at = datetime.fromtimestamp(float(msg_date), tz=timezone.utc).isoformat()
    else:
        created_at = datetime.now(tz=timezone.utc).isoformat()

    return {
        "chat_id": str(chat_id),
        "message_id": message_id,
        "request_text": text,
        "created_at": created_at,
        "correlation_id": f"tg-{update.get('update_id', 'unknown')}-{uuid4().hex[:8]}",
    }



def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    backend_base = os.getenv("BACKEND_BASE_URL", DEFAULT_BACKEND_BASE)
    telegram_base = os.getenv("TELEGRAM_API_BASE", DEFAULT_TELEGRAM_BASE)

    print(f"env TELEGRAM_BOT_TOKEN set: {bool(token)}")
    print(f"env BACKEND_BASE_URL set: {bool(os.getenv('BACKEND_BASE_URL'))}")
    print(f"env TELEGRAM_API_BASE set: {bool(os.getenv('TELEGRAM_API_BASE'))}")

    if not token:
        print("TELEGRAM_BOT_TOKEN missing")
        return 1

    state = _load_state()
    offset = state["last_update_id"] + 1

    updates_url = f"{telegram_base}/bot{token}/getUpdates"
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(updates_url, params={"offset": offset, "timeout": 0})
            resp.raise_for_status()
            body = resp.json()
            updates = body.get("result", []) if isinstance(body, dict) else []

            accepted = 0
            skipped = 0
            failed = 0
            last_update_id = state["last_update_id"]
            for update in updates:
                if not isinstance(update, dict):
                    skipped += 1
                    continue

                try:
                    update_id = int(update.get("update_id", 0))
                except (TypeError, ValueError):
                    skipped += 1
                    continue

                normalized = _normalize_update(update)
                if normalized is None:
                    # Non-message updates (edited, channel_post, etc.) — safe to skip.
                    if update_id > last_update_id:
                        last_update_id = update_id
                    skipped += 1
                    continue

                webhook_resp = client.post(f"{backend_base}/webhooks/telegram", json=normalized)
                if webhook_resp.status_code >= 300:
                    print(f"ingest failed status={webhook_resp.status_code} update_id={update_id}")
                    failed += 1
                    # Do NOT advance last_update_id — message will be retried on next poll.
                    continue

                # Only advance offset after successful delivery.
                if update_id > last_update_id:
                    last_update_id = update_id
                accepted += 1

            _save_state(last_update_id)
            print(f"updates_total={len(updates)} accepted={accepted} skipped={skipped} failed={failed}")
            return 0
    except Exception as exc:  # pragma: no cover
        print(f"polling error: {type(exc).__name__}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
