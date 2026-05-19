#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[1]
RECORDS_DIR = ROOT / "memory" / "records"
QUARANTINE_DIR = ROOT / "reports" / "memory_recorder" / "quarantine"
KINDS = {"fact", "decision", "todo", "constraint", "incident", "evidence", "status"}
CONFIDENCE = {"low", "medium", "high"}
MAX_SUMMARY_CHARS = 1000
MAX_SOURCE_REFS = 64
MAX_SOURCE_REF_CHARS = 2048
MAX_FACT_PAYLOAD_BYTES = 32768


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _safe_scope(scope: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._/-]+", "_", str(scope or "").strip())
    normalized = normalized.strip("./")
    return normalized[:160] or "global"


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


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid4().hex[:6]}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _truncate_for_quarantine(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return "<max-depth>"
    if isinstance(value, dict):
        items = list(value.items())[:32]
        return {str(key)[:120]: _truncate_for_quarantine(item, depth=depth + 1) for key, item in items}
    if isinstance(value, list):
        return [_truncate_for_quarantine(item, depth=depth + 1) for item in value[:20]]
    if isinstance(value, str):
        return value[:512] + ("...<truncated>" if len(value) > 512 else "")
    return value


def quarantine_record(raw: Any, errors: list[str]) -> Path:
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    scope = _safe_scope((raw or {}).get("scope", "global")) if isinstance(raw, dict) else "global"
    path = QUARANTINE_DIR / scope / f"{timestamp}-{uuid4().hex[:8]}.json"
    payload = {
        "kind": "memory_append_quarantine",
        "ts": utc_now_iso(),
        "scope": scope,
        "errors": list(errors),
        "raw_payload": _truncate_for_quarantine(raw),
    }
    if isinstance(raw, dict):
        try:
            payload["raw_payload_bytes"] = len(json.dumps(raw, ensure_ascii=True, sort_keys=True).encode("utf-8"))
        except Exception:
            payload["raw_payload_bytes"] = -1
    _atomic_write_json(path, payload)
    return path


class MemoryRecordValidationError(ValueError):
    def __init__(self, errors: list[str], quarantine_path: Path):
        self.errors = list(errors)
        self.quarantine_path = quarantine_path
        super().__init__(
            "invalid_memory_record:" + ",".join(self.errors) + f"; quarantine_ref={self.quarantine_path}"
        )


def validate_record(raw: dict[str, Any]) -> list[str]:
    if not isinstance(raw, dict):
        return ["record_must_be_object"]
    errors: list[str] = []
    if not str(raw.get("kind", "")).strip():
        errors.append("missing:kind")
    if not str(raw.get("summary", "")).strip():
        errors.append("missing:summary")
    elif len(str(raw.get("summary", "")).strip()) > MAX_SUMMARY_CHARS:
        errors.append("summary_too_large")
    if not isinstance(raw.get("fact_payload", {}), dict):
        errors.append("invalid:fact_payload")
    else:
        try:
            fact_payload_bytes = len(json.dumps(raw.get("fact_payload", {}), ensure_ascii=True, sort_keys=True).encode("utf-8"))
        except Exception:
            fact_payload_bytes = MAX_FACT_PAYLOAD_BYTES + 1
        if fact_payload_bytes > MAX_FACT_PAYLOAD_BYTES:
            errors.append("fact_payload_too_large")
    source_refs = raw.get("source_refs", [])
    if not isinstance(source_refs, list):
        errors.append("invalid:source_refs")
    else:
        if len(source_refs) > MAX_SOURCE_REFS:
            errors.append("too_many_source_refs")
        if any(len(str(item).strip()) > MAX_SOURCE_REF_CHARS for item in source_refs):
            errors.append("source_ref_too_large")
    confidence = str(raw.get("confidence", "")).strip().lower() or "medium"
    if confidence not in CONFIDENCE:
        errors.append("invalid:confidence")
    kind = str(raw.get("kind", "")).strip().lower()
    if kind and kind not in KINDS:
        errors.append("invalid:kind")
    return errors


def normalize_record(raw: dict[str, Any]) -> dict[str, Any]:
    confidence = str(raw.get("confidence", "medium")).strip().lower() or "medium"
    if confidence not in CONFIDENCE:
        confidence = "medium"
    return {
        "record_id": str(raw.get("record_id", "")).strip() or f"mem-{uuid4().hex[:12]}",
        "ts": str(raw.get("ts", "")).strip() or utc_now_iso(),
        "kind": str(raw.get("kind", "fact")).strip().lower() or "fact",
        "scope": _safe_scope(str(raw.get("scope", "global")).strip() or "global"),
        "summary": str(raw.get("summary", "")).strip(),
        "fact_payload": raw.get("fact_payload", {}) if isinstance(raw.get("fact_payload"), dict) else {},
        "source_refs": [str(item).strip() for item in raw.get("source_refs", []) if str(item).strip()],
        "trace_id": str(raw.get("trace_id", "")).strip(),
        "task_id": str(raw.get("task_id", "")).strip(),
        "author": str(raw.get("author", "")).strip() or "agn",
        "confidence": confidence,
    }


def append_record(raw: dict[str, Any]) -> dict[str, Any]:
    record = normalize_record(raw)
    errors = validate_record(record)
    if errors:
        quarantine_path = quarantine_record(raw, errors)
        raise MemoryRecordValidationError(errors, quarantine_path)
    day = record["ts"][:10]
    target = RECORDS_DIR / record["scope"] / f"{day}.jsonl"
    _append_jsonl(target, record)
    return record


def iter_records(*, scope: str = "", limit: int = 0) -> list[dict[str, Any]]:
    """Iterate memory records with lazy line-by-line loading to avoid
    reading entire JSONL files into memory at once."""
    root = RECORDS_DIR / _safe_scope(scope) if scope else RECORDS_DIR
    results: list[dict[str, Any]] = []
    if not root.exists():
        return results
    for path in sorted(root.rglob("*.jsonl")):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        results.append(payload)
                        if limit > 0 and len(results) >= limit:
                            return results
        except OSError:
            continue
    return results


_ISO_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def query_agent_findings(
    *,
    author: str = "",
    kind: str = "",
    scope: str = "",
    limit: int = 20,
    since: str = "",
) -> list[dict[str, Any]]:
    """Query memory records by author, kind, scope, and time.

    Enables cross-agent memory sharing: one agent can read findings
    written by another. Example:
        query_agent_findings(author="natsura", kind="fact", limit=10)

    The ``since`` parameter must be a valid ISO 8601 prefix
    (e.g. ``2026-03-15`` or ``2026-03-15T10:00:00``).  Invalid values
    are silently ignored (treated as no filter).
    """
    # Validate since — must look like an ISO date prefix
    effective_since = ""
    if since:
        since_stripped = str(since).strip()
        if _ISO_PREFIX_RE.match(since_stripped):
            effective_since = since_stripped
        else:
            import sys as _sys
            print(f"[memory_recorder] WARN: ignoring invalid 'since' value: {since_stripped!r}", file=_sys.stderr)

    all_records = iter_records(scope=scope, limit=0)
    filtered: list[dict[str, Any]] = []
    for rec in all_records:
        if author and str(rec.get("author", "")).strip().lower() != author.strip().lower():
            continue
        if kind and str(rec.get("kind", "")).strip().lower() != kind.strip().lower():
            continue
        if effective_since and str(rec.get("ts", "")) < effective_since:
            continue
        filtered.append(rec)
    # Return most recent first, capped at limit.
    filtered.sort(key=lambda r: str(r.get("ts", "")), reverse=True)
    return filtered[:limit] if limit > 0 else filtered
