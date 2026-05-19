"""AGN runtime message bus.

This is the real package implementation for AGN's message bus, acknowledgements,
dead-letter handling, and message expiration.
The legacy script remains only as a compatibility shim.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any
from uuid import uuid4

try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from agn.dispatch.event_store import append_event


PACKAGE_PATH = "agn.dispatch.bus"
LEGACY_SCRIPT_SHIM = "scripts/runtime_bus.py"
BUS_ROOT = ROOT / "runtime" / "bus"
MESSAGES_DIR = BUS_ROOT / "messages"
ACKS_DIR = BUS_ROOT / "acks"
TOPICS_DIR = BUS_ROOT / "topics"
DEAD_LETTER_DIR = BUS_ROOT / "dead_letter"
INDEX_PATH = BUS_ROOT / "index.jsonl"

PRIORITIES = {"low", "medium", "high"}
ACK_STATUSES = {"not_required", "pending", "acked", "expired", "dead_letter"}
DELIVERY_STATUSES = {"pending", "delivered", "expired", "dead_letter"}
BUS_LOCK_PATH = BUS_ROOT / ".bus_write.lock"


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def ensure_bus_dirs() -> None:
    for path in (MESSAGES_DIR, ACKS_DIR, TOPICS_DIR, DEAD_LETTER_DIR):
        path.mkdir(parents=True, exist_ok=True)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
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


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class _BusWriteLock:
    """Advisory cross-file lock for multi-step bus mutations."""

    def __init__(self) -> None:
        self._handle: Any = None

    def __enter__(self) -> "_BusWriteLock":
        BUS_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._handle = BUS_LOCK_PATH.open("a")
        if fcntl is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *_: Any) -> None:
        if self._handle is not None:
            if fcntl is not None:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


def _safe_name(value: str, *, default: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(value or "").strip())
    return normalized[:120] or default


def _parse_iso(value: str) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def validate_message(raw: dict[str, Any]) -> list[str]:
    if not isinstance(raw, dict):
        return ["message_must_be_object"]
    errors: list[str] = []
    for key in ("from", "to", "type", "topic", "summary", "payload_ref"):
        if not str(raw.get(key, "")).strip():
            errors.append(f"missing:{key}")
    priority = str(raw.get("priority", "medium")).strip().lower() or "medium"
    if priority not in PRIORITIES:
        errors.append("invalid:priority")
    ttl = raw.get("ttl_sec", 0)
    try:
        ttl_value = int(ttl or 0)
        if ttl_value < 0:
            errors.append("invalid:ttl_sec")
    except (TypeError, ValueError):
        errors.append("invalid:ttl_sec")
    return errors


def normalize_message(raw: dict[str, Any]) -> dict[str, Any]:
    ack_required = bool(raw.get("ack_required", False))
    priority = str(raw.get("priority", "medium")).strip().lower() or "medium"
    if priority not in PRIORITIES:
        priority = "medium"
    ttl_sec = max(0, int(raw.get("ttl_sec", 0) or 0))
    message_id = str(raw.get("id", "")).strip() or f"bus-{uuid4().hex[:12]}"
    return {
        "id": message_id,
        "ts": str(raw.get("ts", "")).strip() or utc_now_iso(),
        "from": str(raw.get("from", "")).strip(),
        "to": str(raw.get("to", "")).strip(),
        "type": str(raw.get("type", "")).strip(),
        "topic": str(raw.get("topic", "")).strip(),
        "summary": str(raw.get("summary", "")).strip(),
        "payload_ref": str(raw.get("payload_ref", "")).strip(),
        "priority": priority,
        "ttl_sec": ttl_sec,
        "ack_required": ack_required,
        "ack_status": "pending" if ack_required else "not_required",
        "related_task": str(raw.get("related_task", "")).strip(),
        "related_trace": str(raw.get("related_trace", "")).strip(),
        "related_project": str(raw.get("related_project", "")).strip(),
        "reply_to": str(raw.get("reply_to", "")).strip(),
        "delivery_status": str(raw.get("delivery_status", "pending")).strip() or "pending",
    }


def _index_entry(kind: str, message: dict[str, Any], **extra: Any) -> dict[str, Any]:
    payload = {
        "kind": kind,
        "ts": utc_now_iso(),
        "message_id": str(message.get("id", "")).strip(),
        "topic": str(message.get("topic", "")).strip(),
        "from": str(message.get("from", "")).strip(),
        "to": str(message.get("to", "")).strip(),
        "related_task": str(message.get("related_task", "")).strip(),
        "related_trace": str(message.get("related_trace", "")).strip(),
    }
    payload.update(extra)
    return payload


def _persist_event(event_type: str, message: dict[str, Any], **payload: Any) -> None:
    trace_id = str(message.get("related_trace", "")).strip()
    task_id = str(message.get("related_task", "")).strip()
    if not trace_id or not task_id:
        return
    append_event(
        trace_id=trace_id,
        task_id=task_id,
        event_type=event_type,
        payload={"message_id": str(message.get("id", "")).strip(), **payload},
    )


def publish_message(raw: dict[str, Any], *, persist_event: bool = True) -> dict[str, Any]:
    ensure_bus_dirs()
    message = normalize_message(raw)
    errors = validate_message(message)
    if errors:
        raise ValueError("invalid_bus_message:" + ",".join(errors))
    target = MESSAGES_DIR / f"{_safe_name(message['id'], default='message')}.json"
    with _BusWriteLock():
        if target.exists():
            raise ValueError(f"message_id_exists:{message['id']}")
        _atomic_write_json(target, message)
        _append_jsonl(INDEX_PATH, _index_entry("message", message, delivery_status=message["delivery_status"], ack_status=message["ack_status"]))
        topic_path = TOPICS_DIR / f"{_safe_name(message['topic'], default='topic')}.jsonl"
        _append_jsonl(topic_path, _index_entry("message", message, payload_ref=message["payload_ref"]))
    if persist_event:
        _persist_event("BUS_MESSAGE_PUBLISHED", message, payload_ref=message["payload_ref"], message_type=message["type"])
    return message


def load_message(message_id: str) -> dict[str, Any]:
    path = MESSAGES_DIR / f"{_safe_name(message_id, default='message')}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _ack_files(message_id: str) -> list[Path]:
    safe = _safe_name(message_id, default="message")
    return sorted(ACKS_DIR.glob(f"{safe}.*.json"))


def _dead_letter_files(message_id: str) -> list[Path]:
    safe = _safe_name(message_id, default="message")
    return sorted(DEAD_LETTER_DIR.glob(f"{safe}.*.json"))


def effective_message_state(message_id: str, *, now: datetime | None = None) -> dict[str, str]:
    message = load_message(message_id)
    current = now or datetime.now(tz=timezone.utc)
    if _dead_letter_files(message_id):
        return {"ack_status": "dead_letter", "delivery_status": "dead_letter"}
    if _ack_files(message_id):
        return {"ack_status": "acked", "delivery_status": "delivered"}
    ttl_sec = int(message.get("ttl_sec", 0) or 0)
    if ttl_sec > 0:
        created_at = _parse_iso(str(message.get("ts", "")))
        if created_at + timedelta(seconds=ttl_sec) <= current:
            return {"ack_status": "expired", "delivery_status": "expired"}
    if bool(message.get("ack_required")):
        return {"ack_status": "pending", "delivery_status": "pending"}
    return {"ack_status": "not_required", "delivery_status": "delivered"}


def acknowledge_message(
    message_id: str,
    *,
    actor: str,
    note: str = "",
    persist_event: bool = True,
) -> dict[str, Any]:
    ensure_bus_dirs()
    message = load_message(message_id)
    ack = {
        "message_id": str(message_id).strip(),
        "ts": utc_now_iso(),
        "actor": str(actor or "").strip() or "unknown",
        "ack_status": "acked",
        "note": str(note or "").strip(),
    }
    ack_path = ACKS_DIR / f"{_safe_name(message_id, default='message')}.{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json"
    with _BusWriteLock():
        _atomic_write_json(ack_path, ack)
        _append_jsonl(INDEX_PATH, _index_entry("ack", message, actor=ack["actor"], ack_status="acked"))
    if persist_event:
        _persist_event("BUS_MESSAGE_ACKED", message, actor=ack["actor"])
    return ack


def expire_messages(*, now: datetime | None = None, persist_event: bool = True) -> list[dict[str, Any]]:
    ensure_bus_dirs()
    current = now or datetime.now(tz=timezone.utc)
    expired: list[dict[str, Any]] = []
    for path in sorted(MESSAGES_DIR.glob("*.json")):
        message = json.loads(path.read_text(encoding="utf-8"))
        ttl_sec = int(message.get("ttl_sec", 0) or 0)
        if ttl_sec <= 0:
            continue
        state = effective_message_state(str(message.get("id", "")).strip(), now=current)
        if state["ack_status"] in {"acked", "dead_letter"}:
            continue
        created_at = _parse_iso(str(message.get("ts", "")))
        if created_at + timedelta(seconds=ttl_sec) > current:
            continue
        dead_letter = {
            "message_id": str(message.get("id", "")).strip(),
            "ts": utc_now_iso(),
            "reason": "ttl_expired",
            "message": message,
        }
        dead_letter_path = DEAD_LETTER_DIR / f"{_safe_name(dead_letter['message_id'], default='message')}.ttl_expired.json"
        written = False
        with _BusWriteLock():
            if not dead_letter_path.exists():
                _atomic_write_json(dead_letter_path, dead_letter)
                _append_jsonl(
                    INDEX_PATH,
                    _index_entry(
                        "dead_letter",
                        message,
                        reason="ttl_expired",
                        ack_status="expired",
                        delivery_status="dead_letter",
                    ),
                )
                topic_path = TOPICS_DIR / f"{_safe_name(str(message.get('topic', 'topic')), default='topic')}.jsonl"
                _append_jsonl(topic_path, _index_entry("dead_letter", message, reason="ttl_expired"))
                written = True
        if written and persist_event:
            _persist_event("BUS_MESSAGE_EXPIRED", message, reason="ttl_expired")
        expired.append(dead_letter)
    return expired


def iter_messages(*, topic: str = "", recipient: str = "") -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for path in sorted(MESSAGES_DIR.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if topic and str(payload.get("topic", "")).strip() != topic:
            continue
        if recipient and str(payload.get("to", "")).strip() != recipient:
            continue
        messages.append(payload)
    return messages
