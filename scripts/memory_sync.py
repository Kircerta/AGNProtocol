#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import tempfile
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
MEMORY_DIR = ROOT / "memory"
EVENTS_DIR = MEMORY_DIR / "events"
STATE_DIR = MEMORY_DIR / "state"
INSTANCES_DIR = MEMORY_DIR / "instances"
CONFLICTS_DIR = MEMORY_DIR / "conflicts"


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _sanitize_instance_id(raw: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(raw or "").strip())
    text = text.strip("-._")
    return text[:64] or "agn-node"


def resolve_instance_id() -> str:
    env_id = str(os.getenv("AGN_INSTANCE_ID", "")).strip()
    if env_id:
        return _sanitize_instance_id(env_id)
    return _sanitize_instance_id(socket.gethostname())


def _parse_iso(raw: str) -> datetime:
    text = str(raw or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _hash_payload(payload: Any) -> str:
    import hashlib

    return hashlib.sha256(_json_dumps(payload).encode("utf-8")).hexdigest()


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
            handle.write(_json_dumps(payload) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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


def ensure_instance_metadata(instance_id: str | None = None) -> Path:
    iid = _sanitize_instance_id(instance_id or resolve_instance_id())
    path = INSTANCES_DIR / f"{iid}.json"
    existing = load_json_or_default(path, {})
    payload = {
        "instance_id": iid,
        "hostname": socket.gethostname(),
        "last_seen_at": utc_now_iso(),
    }
    if isinstance(existing, dict):
        created = str(existing.get("created_at", "")).strip()
        payload["created_at"] = created or payload["last_seen_at"]
    else:
        payload["created_at"] = payload["last_seen_at"]
    atomic_write_json(path, payload)
    return path


def _state_path(instance_id: str) -> Path:
    return STATE_DIR / f"{_sanitize_instance_id(instance_id)}.json"


def _next_clock(instance_id: str) -> int:
    path = _state_path(instance_id)
    state = load_json_or_default(path, {"instance_id": instance_id, "last_clock": 0})
    last = int(state.get("last_clock", 0) or 0)
    nxt = last + 1
    state["instance_id"] = instance_id
    state["last_clock"] = nxt
    state["updated_at"] = utc_now_iso()
    atomic_write_json(path, state)
    return nxt


def append_memory_event(
    *,
    key: str,
    payload: Any,
    kind: str,
    source: str = "agn",
    task_id: str = "",
    correlation_id: str = "",
    instance_id: str | None = None,
) -> dict[str, Any]:
    iid = _sanitize_instance_id(instance_id or resolve_instance_id())
    if not str(key or "").strip():
        raise ValueError("memory key is required")
    ensure_instance_metadata(iid)
    clock = _next_clock(iid)
    ts = utc_now_iso()
    event = {
        "event_id": f"{iid}:{clock}:{uuid4().hex[:12]}",
        "event_version": 1,
        "instance_id": iid,
        "logical_clock": clock,
        "ts": ts,
        "source": str(source or "agn").strip() or "agn",
        "kind": str(kind or "memory_event").strip() or "memory_event",
        "key": str(key).strip(),
        "task_id": str(task_id or "").strip(),
        "correlation_id": str(correlation_id or "").strip(),
        "payload": payload,
        "payload_sha256": _hash_payload(payload),
    }
    day = _parse_iso(ts).strftime("%Y-%m-%d")
    append_jsonl(EVENTS_DIR / iid / f"{day}.jsonl", event)
    return event


def iter_memory_events(*, since_date: str = "", max_events: int = 0) -> Any:
    """Yield memory events one at a time to avoid loading all into memory.

    Args:
        since_date: Only process JSONL files dated on or after this date (YYYY-MM-DD).
        max_events: Stop after yielding this many events. 0 = unlimited.
    """
    if not EVENTS_DIR.exists():
        return
    yielded = 0
    for path in sorted(EVENTS_DIR.glob("*/*.jsonl")):
        if since_date:
            # JSONL files are named YYYY-MM-DD.jsonl; skip older files.
            file_date = path.stem  # e.g. "2026-02-28"
            if file_date < since_date:
                continue
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except Exception:
                    continue
                if isinstance(payload, dict):
                    yield payload
                    yielded += 1
                    if max_events and yielded >= max_events:
                        return


def load_all_memory_events(*, since_date: str = "", max_events: int = 0) -> list[dict[str, Any]]:
    """Load memory events into a list. Prefer iter_memory_events for large datasets."""
    return list(iter_memory_events(since_date=since_date, max_events=max_events))


def _candidate_from_event(event: dict[str, Any]) -> dict[str, Any] | None:
    key = str(event.get("key", "")).strip()
    if not key:
        return None
    payload = event.get("payload")
    return {
        "key": key,
        "payload": payload,
        "payload_sha256": _hash_payload(payload),
        "event_id": str(event.get("event_id", "")).strip(),
        "instance_id": str(event.get("instance_id", "")).strip(),
        "logical_clock": int(event.get("logical_clock", 0) or 0),
        "ts": str(event.get("ts", "")).strip(),
        "kind": str(event.get("kind", "")).strip(),
        "source": str(event.get("source", "")).strip(),
        "task_id": str(event.get("task_id", "")).strip(),
        "correlation_id": str(event.get("correlation_id", "")).strip(),
    }


def _candidate_rank(candidate: dict[str, Any]) -> tuple[datetime, int, str, str]:
    return (
        _parse_iso(str(candidate.get("ts", ""))),
        int(candidate.get("logical_clock", 0) or 0),
        str(candidate.get("instance_id", "")),
        str(candidate.get("event_id", "")),
    )


def merge_memory_events(events: Any) -> dict[str, Any]:
    """Merge memory events using LWW. Accepts list or iterable."""
    latest: dict[str, dict[str, Any]] = {}
    conflicts: dict[str, dict[str, Any]] = {}
    valid = 0
    total = 0

    for event in events:
        total += 1
        if not isinstance(event, dict):
            continue
        candidate = _candidate_from_event(event)
        if candidate is None:
            continue
        valid += 1
        key = candidate["key"]
        current = latest.get(key)
        if current is None:
            latest[key] = candidate
            continue

        same_payload = current.get("payload_sha256") == candidate.get("payload_sha256")
        if _candidate_rank(candidate) >= _candidate_rank(current):
            winner, loser = candidate, current
            latest[key] = candidate
        else:
            winner, loser = current, candidate

        if same_payload:
            continue

        # P1-8 fix: preserve loser payload for conflict recovery.
        conflict_id = f"{key}:{winner.get('event_id','')}:{loser.get('event_id','')}"
        conflicts[conflict_id] = {
            "conflict_id": conflict_id,
            "key": key,
            "winner_event_id": str(winner.get("event_id", "")),
            "winner_instance_id": str(winner.get("instance_id", "")),
            "winner_ts": str(winner.get("ts", "")),
            "winner_payload_sha256": str(winner.get("payload_sha256", "")),
            "loser_event_id": str(loser.get("event_id", "")),
            "loser_instance_id": str(loser.get("instance_id", "")),
            "loser_ts": str(loser.get("ts", "")),
            "loser_payload_sha256": str(loser.get("payload_sha256", "")),
            "loser_payload": loser.get("payload"),
            "resolution": "lww",
        }

    latest_by_key = dict(sorted(latest.items(), key=lambda item: item[0]))
    conflicts_list = sorted(conflicts.values(), key=lambda item: item["conflict_id"])
    return {
        "total_events": total,
        "valid_events": valid,
        "distinct_keys": len(latest_by_key),
        "latest_by_key": latest_by_key,
        "conflicts": conflicts_list,
    }


def write_conflicts_snapshot(conflicts: list[dict[str, Any]], *, instance_id: str | None = None) -> Path:
    """Write conflict snapshot, preserving older conflicts via append-merge.

    P2-2: Previous implementation overwrote the file, losing historical
    conflict records across merge cycles.  Now loads existing conflicts,
    deduplicates by event_id, and writes the combined set.
    """
    iid = _sanitize_instance_id(instance_id or resolve_instance_id())
    path = CONFLICTS_DIR / f"{iid}.json"

    # Load existing conflicts for append-merge.
    existing_conflicts: list[dict[str, Any]] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing.get("conflicts"), list):
                existing_conflicts = existing["conflicts"]
        except (json.JSONDecodeError, OSError):
            pass

    # Deduplicate by event_id.
    seen_ids: set[str] = set()
    merged: list[dict[str, Any]] = []
    for c in existing_conflicts + conflicts:
        eid = str(c.get("event_id", ""))
        if eid and eid in seen_ids:
            continue
        if eid:
            seen_ids.add(eid)
        merged.append(c)

    payload = {
        "instance_id": iid,
        "resolution_policy": "lww",
        "conflict_count": len(merged),
        "conflicts": merged,
    }
    atomic_write_json(path, payload)
    return path


def _write_merge_output(payload: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output_path, payload)


def cmd_append(args: argparse.Namespace) -> int:
    try:
        payload: Any = json.loads(args.payload_json)
    except Exception:
        payload = {"text": str(args.payload_json)}
    event = append_memory_event(
        key=args.key,
        payload=payload,
        kind=args.kind,
        source=args.source,
        task_id=args.task_id,
        correlation_id=args.correlation_id,
        instance_id=args.instance_id,
    )
    print(json.dumps({"ok": True, "event": event}, ensure_ascii=True))
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    since_date = str(getattr(args, "since_date", "") or "").strip()
    merged = merge_memory_events(iter_memory_events(since_date=since_date))
    write_conflicts_snapshot(merged["conflicts"], instance_id=args.instance_id)
    if args.output:
        _write_merge_output(merged, Path(args.output))
    print(
        json.dumps(
            {
                "ok": True,
                "total_events": merged["total_events"],
                "distinct_keys": merged["distinct_keys"],
                "conflict_count": len(merged["conflicts"]),
            },
            ensure_ascii=True,
        )
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    iid = _sanitize_instance_id(args.instance_id or resolve_instance_id())
    state = load_json_or_default(_state_path(iid), {"last_clock": 0})
    events = list((EVENTS_DIR / iid).glob("*.jsonl")) if (EVENTS_DIR / iid).exists() else []
    print(
        json.dumps(
            {
                "ok": True,
                "instance_id": iid,
                "event_files": len(events),
                "last_clock": int(state.get("last_clock", 0) or 0),
            },
            ensure_ascii=True,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AGN multi-instance memory event and merge utility")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_append = sub.add_parser("append")
    p_append.add_argument("--key", required=True)
    p_append.add_argument("--kind", required=True)
    p_append.add_argument("--payload-json", required=True)
    p_append.add_argument("--source", default="agn")
    p_append.add_argument("--task-id", default="")
    p_append.add_argument("--correlation-id", default="")
    p_append.add_argument("--instance-id", default="")

    p_merge = sub.add_parser("merge")
    p_merge.add_argument("--output", default="")
    p_merge.add_argument("--instance-id", default="")
    p_merge.add_argument("--since-date", default="", help="Only merge events from YYYY-MM-DD onward")

    p_status = sub.add_parser("status")
    p_status.add_argument("--instance-id", default="")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.cmd == "append":
        return cmd_append(args)
    if args.cmd == "merge":
        return cmd_merge(args)
    return cmd_status(args)


if __name__ == "__main__":
    raise SystemExit(main())
