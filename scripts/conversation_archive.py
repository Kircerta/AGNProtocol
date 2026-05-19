#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from admin_control_common import atomic_write_json, append_jsonl, load_json
from provider_registry import load_registry, probe_capabilities
from agn.governance.review_contract import extract_json_object


DEFAULT_CONFIG_PATH = ROOT / "agn2" / "conversation_archive" / "config.json"
DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "scope": "codex_admin",
    "sources": {
        "codex_homes": [
            str(Path.home() / ".codex"),
            str(Path.home() / ".codex_agn"),
        ]
    },
    "storage": {
        "primary_db_path": "runtime/conversation_archive/conversation_archive.sqlite",
        "spool_path": "runtime/conversation_archive/spool/pending.jsonl",
        "state_path": "runtime/conversation_archive/state/offsets.json",
        "status_path": "runtime/conversation_archive/status.json",
        "audit_path": "runtime/conversation_archive/audit.jsonl",
        "export_dir": "runtime/conversation_archive/exports",
        "review_input_dir": "runtime/conversation_archive/review_inputs",
        "review_report_dir": "reports/conversation_archive",
    },
    "poll_interval_seconds": 30.0,
    "review": {
        "enabled": True,
        "cadence_hours": 72,
        "preferred_providers": ["claude", "gemini"],
        "claude_model": "opus",
        "gemini_model": "pro",
    },
}
REVIEW_PROVIDER_ORDER = ("claude", "gemini")
FLAGSHIP_REVIEWER_LABELS = {
    "claude": "Claude Opus 4.6",
    "gemini": "Gemini 3.1 Pro",
}


@dataclass
class IngestRecord:
    session: dict[str, Any]
    message: dict[str, Any]


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def local_now() -> datetime:
    return datetime.now().astimezone()


def parse_iso_ts(value: str) -> datetime:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    return datetime.fromisoformat(raw)


def stable_hash(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8", errors="replace"))
        digest.update(b"\x1f")
    return digest.hexdigest()


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def resolve_path(value: str) -> Path:
    path = Path(str(value or "")).expanduser()
    if path.is_absolute():
        return path
    return (ROOT / path).resolve()


def load_config(path: Path | None = None) -> dict[str, Any]:
    if path is not None:
        config_path = path
    else:
        raw_override = str(os.getenv("AGN_CONVERSATION_ARCHIVE_CONFIG", "")).strip()
        config_path = Path(raw_override).expanduser() if raw_override else DEFAULT_CONFIG_PATH
    payload = load_json(Path(config_path), default=DEFAULT_CONFIG)
    config = deep_merge(DEFAULT_CONFIG, payload)
    storage = config.get("storage", {})
    if isinstance(storage, dict):
        for key in ("primary_db_path", "spool_path", "state_path", "status_path", "audit_path", "export_dir", "review_input_dir", "review_report_dir"):
            storage[key] = str(resolve_path(str(storage.get(key, ""))))
    sources = config.get("sources", {})
    if isinstance(sources, dict):
        homes = []
        for item in sources.get("codex_homes", []):
            text = str(item).strip()
            if text:
                homes.append(str(Path(text).expanduser().resolve()))
        sources["codex_homes"] = homes
    review_cfg = config.get("review", {})
    if isinstance(review_cfg, dict):
        preferred = [
            str(item).strip().lower()
            for item in review_cfg.get("preferred_providers", [])
            if str(item).strip()
        ]
        preferred = [item for item in preferred if item in REVIEW_PROVIDER_ORDER]
        review_cfg["preferred_providers"] = preferred or list(REVIEW_PROVIDER_ORDER)
    return config


def storage_paths(config: dict[str, Any]) -> dict[str, Path]:
    storage = config.get("storage", {}) if isinstance(config.get("storage"), dict) else {}
    return {
        "primary_db_path": Path(str(storage.get("primary_db_path"))),
        "spool_path": Path(str(storage.get("spool_path"))),
        "state_path": Path(str(storage.get("state_path"))),
        "status_path": Path(str(storage.get("status_path"))),
        "audit_path": Path(str(storage.get("audit_path"))),
        "export_dir": Path(str(storage.get("export_dir"))),
        "review_input_dir": Path(str(storage.get("review_input_dir"))),
        "review_report_dir": Path(str(storage.get("review_report_dir"))),
    }


def ensure_runtime_dirs(config: dict[str, Any]) -> None:
    paths = storage_paths(config)
    for key in ("spool_path", "state_path", "status_path", "audit_path"):
        paths[key].parent.mkdir(parents=True, exist_ok=True)
    paths["export_dir"].mkdir(parents=True, exist_ok=True)
    paths["review_input_dir"].mkdir(parents=True, exist_ok=True)
    paths["review_report_dir"].mkdir(parents=True, exist_ok=True)


def audit(config: dict[str, Any], action: str, **extra: Any) -> None:
    payload = {"ts": utc_now_iso(), "action": str(action).strip(), **extra}
    append_jsonl(storage_paths(config)["audit_path"], payload)


def load_local_state(config: dict[str, Any]) -> dict[str, Any]:
    state_path = storage_paths(config)["state_path"]
    return load_json(state_path, default={"files": {}, "last_scan_at": "", "last_review_completed_at": ""})


def save_local_state(config: dict[str, Any], payload: dict[str, Any]) -> None:
    atomic_write_json(storage_paths(config)["state_path"], payload)


def update_status(config: dict[str, Any], payload: dict[str, Any]) -> None:
    atomic_write_json(storage_paths(config)["status_path"], payload)


def mount_root_for(path: Path) -> Path | None:
    parts = path.resolve().parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        return Path("/", "Volumes", parts[2])
    return None


def primary_storage_available(config: dict[str, Any]) -> bool:
    db_path = storage_paths(config)["primary_db_path"]
    mount_root = mount_root_for(db_path)
    if mount_root is not None and not mount_root.exists():
        return False
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return False
    return True


def sqlite_connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS source_sessions (
            session_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            thread_name TEXT NOT NULL,
            codex_home TEXT NOT NULL,
            rollout_path TEXT NOT NULL,
            first_ts TEXT NOT NULL,
            last_ts TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            message_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            role TEXT NOT NULL,
            speaker TEXT NOT NULL,
            body TEXT NOT NULL,
            body_hash TEXT NOT NULL,
            local_date TEXT NOT NULL,
            raw_ref TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES source_sessions(session_id)
        );

        CREATE TABLE IF NOT EXISTS archive_instances (
            instance_id TEXT PRIMARY KEY,
            local_date TEXT NOT NULL,
            source_scope TEXT NOT NULL,
            message_count INTEGER NOT NULL,
            session_count INTEGER NOT NULL,
            time_start TEXT NOT NULL,
            time_end TEXT NOT NULL,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(local_date, source_scope)
        );

        CREATE TABLE IF NOT EXISTS instance_message_map (
            instance_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            PRIMARY KEY(instance_id, message_id),
            FOREIGN KEY(instance_id) REFERENCES archive_instances(instance_id),
            FOREIGN KEY(message_id) REFERENCES messages(message_id)
        );

        CREATE TABLE IF NOT EXISTS review_runs (
            review_run_id TEXT PRIMARY KEY,
            instance_scope TEXT NOT NULL,
            provider TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            status TEXT NOT NULL,
            report_ref TEXT NOT NULL,
            summary TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS review_findings (
            review_run_id TEXT NOT NULL,
            finding_type TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            content TEXT NOT NULL,
            PRIMARY KEY(review_run_id, finding_type, ordinal),
            FOREIGN KEY(review_run_id) REFERENCES review_runs(review_run_id)
        );

        CREATE TABLE IF NOT EXISTS ingest_state (
            rollout_path TEXT PRIMARY KEY,
            source_home TEXT NOT NULL,
            offset INTEGER NOT NULL,
            inode INTEGER NOT NULL,
            size INTEGER NOT NULL,
            line_number INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            message_id UNINDEXED,
            body
        );
        """
    )
    conn.commit()


def discover_thread_names(codex_home: Path) -> dict[str, str]:
    mapping: dict[str, str] = {}
    index_path = codex_home / "session_index.jsonl"
    if not index_path.exists():
        return mapping
    for raw_line in index_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        session_id = str(payload.get("id", "")).strip()
        if not session_id:
            continue
        mapping[session_id] = str(payload.get("thread_name", "")).strip()
    return mapping


def extract_message_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    for item in content:
        if not isinstance(item, dict):
            continue
        if str(item.get("type", "")).strip() in {"input_text", "output_text"}:
            text = str(item.get("text", "")).strip()
            if text:
                return text
    return ""


def speaker_for_role(role: str) -> str:
    return "admin" if role == "user" else "codex"


def local_date_for_ts(timestamp: str) -> str:
    return parse_iso_ts(timestamp).astimezone().date().isoformat()


def session_id_from_path(path: Path) -> str:
    name = path.name
    if name.startswith("rollout-") and name.endswith(".jsonl"):
        parts = name[:-6].split("-")
        if parts:
            return parts[-1]
    return path.stem


def iter_rollout_files(codex_home: Path, *, days: int = 0) -> list[Path]:
    base = codex_home / "sessions"
    if not base.exists():
        return []
    cutoff = None
    if days > 0:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    files = sorted(base.glob("**/rollout-*.jsonl"))
    if cutoff is None:
        return files
    filtered: list[Path] = []
    for path in files:
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except FileNotFoundError:
            continue
        if modified >= cutoff:
            filtered.append(path)
    return filtered


def read_rollout_delta(
    *,
    path: Path,
    source_home: Path,
    thread_names: dict[str, str],
    state_entry: dict[str, Any] | None,
) -> tuple[list[IngestRecord], dict[str, Any]]:
    stats = path.stat()
    current_inode = int(stats.st_ino)
    current_size = int(stats.st_size)
    saved = dict(state_entry or {})
    offset = int(saved.get("offset", 0) or 0)
    line_number = int(saved.get("line_number", 0) or 0)
    session_id = str(saved.get("session_id", "")).strip() or session_id_from_path(path)
    if int(saved.get("inode", 0) or 0) != current_inode or current_size < offset:
        offset = 0
        line_number = 0
    records: list[IngestRecord] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(offset)
        while True:
            raw_line = handle.readline()
            if not raw_line:
                break
            offset = handle.tell()
            line_number += 1
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            event_type = str(payload.get("type", "")).strip()
            top_level_ts = str(payload.get("timestamp", "")).strip()
            if event_type == "session_meta":
                meta = payload.get("payload", {})
                if isinstance(meta, dict):
                    session_id = str(meta.get("id", "")).strip() or session_id
                continue
            if event_type != "response_item":
                continue
            message = payload.get("payload", {})
            if not isinstance(message, dict):
                continue
            if str(message.get("type", "")).strip() != "message":
                continue
            role = str(message.get("role", "")).strip()
            if role not in {"user", "assistant"}:
                continue
            body = extract_message_text(message.get("content"))
            if not body:
                continue
            ts = top_level_ts or utc_now_iso()
            raw_sha = stable_hash(stripped)
            raw_ref_payload = {
                "source_home": str(source_home),
                "rollout_path": str(path),
                "line_number": line_number,
                "raw_sha256": raw_sha,
            }
            effective_session_id = session_id or session_id_from_path(path)
            session = {
                "session_id": effective_session_id,
                "thread_id": effective_session_id,
                "thread_name": thread_names.get(effective_session_id, ""),
                "codex_home": str(source_home),
                "rollout_path": str(path),
                "first_ts": ts,
                "last_ts": ts,
            }
            message_payload = {
                "message_id": stable_hash(str(source_home), str(path), str(line_number), raw_sha),
                "session_id": effective_session_id,
                "ts": ts,
                "role": role,
                "speaker": speaker_for_role(role),
                "body": body,
                "body_hash": stable_hash(body),
                "local_date": local_date_for_ts(ts),
                "raw_ref": json.dumps(raw_ref_payload, ensure_ascii=True, sort_keys=True),
            }
            records.append(IngestRecord(session=session, message=message_payload))
    updated_state = {
        "offset": offset,
        "inode": current_inode,
        "size": current_size,
        "line_number": line_number,
        "session_id": session_id,
        "source_home": str(source_home),
        "last_seen_at": utc_now_iso(),
    }
    return records, updated_state


def upsert_session(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    existing = conn.execute(
        "SELECT first_ts, last_ts, thread_name FROM source_sessions WHERE session_id = ?",
        (payload["session_id"],),
    ).fetchone()
    first_ts = payload["first_ts"]
    last_ts = payload["last_ts"]
    thread_name = str(payload.get("thread_name", "")).strip()
    if existing is not None:
        first_ts = min(str(existing["first_ts"]), first_ts)
        last_ts = max(str(existing["last_ts"]), last_ts)
        if not thread_name:
            thread_name = str(existing["thread_name"])
    conn.execute(
        """
        INSERT INTO source_sessions (
            session_id, thread_id, thread_name, codex_home, rollout_path, first_ts, last_ts, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            thread_id=excluded.thread_id,
            thread_name=excluded.thread_name,
            codex_home=excluded.codex_home,
            rollout_path=excluded.rollout_path,
            first_ts=excluded.first_ts,
            last_ts=excluded.last_ts,
            updated_at=excluded.updated_at
        """,
        (
            payload["session_id"],
            payload["thread_id"],
            thread_name,
            payload["codex_home"],
            payload["rollout_path"],
            first_ts,
            last_ts,
            utc_now_iso(),
        ),
    )


def insert_message(conn: sqlite3.Connection, payload: dict[str, Any]) -> bool:
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO messages (
            message_id, session_id, ts, role, speaker, body, body_hash, local_date, raw_ref, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["message_id"],
            payload["session_id"],
            payload["ts"],
            payload["role"],
            payload["speaker"],
            payload["body"],
            payload["body_hash"],
            payload["local_date"],
            payload["raw_ref"],
            utc_now_iso(),
        ),
    )
    if cursor.rowcount:
        conn.execute(
            "INSERT INTO messages_fts (message_id, body) VALUES (?, ?)",
            (payload["message_id"], payload["body"]),
        )
        return True
    return False


def ensure_instance(conn: sqlite3.Connection, local_date: str, source_scope: str) -> str:
    row = conn.execute(
        "SELECT instance_id FROM archive_instances WHERE local_date = ? AND source_scope = ?",
        (local_date, source_scope),
    ).fetchone()
    if row is not None:
        return str(row["instance_id"])
    instance_id = f"instance-{local_date}-{source_scope}"
    conn.execute(
        """
        INSERT INTO archive_instances (
            instance_id, local_date, source_scope, message_count, session_count, time_start, time_end, status, updated_at
        ) VALUES (?, ?, ?, 0, 0, '', '', 'open', ?)
        """,
        (instance_id, local_date, source_scope, utc_now_iso()),
    )
    return instance_id


def refresh_instance(conn: sqlite3.Connection, local_date: str, source_scope: str) -> None:
    row = conn.execute(
        """
        SELECT
            COUNT(m.message_id) AS message_count,
            COUNT(DISTINCT m.session_id) AS session_count,
            COALESCE(MIN(m.ts), '') AS time_start,
            COALESCE(MAX(m.ts), '') AS time_end
        FROM messages m
        WHERE m.local_date = ?
        """,
        (local_date,),
    ).fetchone()
    if row is None:
        return
    instance_id = ensure_instance(conn, local_date, source_scope)
    conn.execute(
        """
        UPDATE archive_instances
        SET message_count = ?, session_count = ?, time_start = ?, time_end = ?, status = ?, updated_at = ?
        WHERE instance_id = ?
        """,
        (
            int(row["message_count"]),
            int(row["session_count"]),
            str(row["time_start"]),
            str(row["time_end"]),
            "ready",
            utc_now_iso(),
            instance_id,
        ),
    )


def sync_ingest_state_to_db(conn: sqlite3.Connection, file_states: dict[str, Any]) -> None:
    for rollout_path, entry in file_states.items():
        conn.execute(
            """
            INSERT INTO ingest_state (
                rollout_path, source_home, offset, inode, size, line_number, session_id, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(rollout_path) DO UPDATE SET
                source_home=excluded.source_home,
                offset=excluded.offset,
                inode=excluded.inode,
                size=excluded.size,
                line_number=excluded.line_number,
                session_id=excluded.session_id,
                last_seen_at=excluded.last_seen_at
            """,
            (
                rollout_path,
                str(entry.get("source_home", "")),
                int(entry.get("offset", 0) or 0),
                int(entry.get("inode", 0) or 0),
                int(entry.get("size", 0) or 0),
                int(entry.get("line_number", 0) or 0),
                str(entry.get("session_id", "")),
                str(entry.get("last_seen_at", "")),
            ),
        )


def insert_records_into_db(config: dict[str, Any], records: list[IngestRecord], state: dict[str, Any]) -> dict[str, Any]:
    db_path = storage_paths(config)["primary_db_path"]
    conn = sqlite_connect(db_path)
    ensure_schema(conn)
    inserted = 0
    affected_dates: set[str] = set()
    for record in records:
        upsert_session(conn, record.session)
        inserted_now = insert_message(conn, record.message)
        instance_id = ensure_instance(conn, record.message["local_date"], str(config.get("scope", "codex_admin")))
        conn.execute(
            "INSERT OR IGNORE INTO instance_message_map (instance_id, message_id) VALUES (?, ?)",
            (instance_id, record.message["message_id"]),
        )
        if inserted_now:
            inserted += 1
        affected_dates.add(record.message["local_date"])
    for local_date in affected_dates:
        refresh_instance(conn, local_date, str(config.get("scope", "codex_admin")))
    sync_ingest_state_to_db(conn, state.get("files", {}))
    conn.commit()
    conn.close()
    return {"ok": True, "inserted_messages": inserted, "affected_dates": sorted(affected_dates)}


def spool_records(config: dict[str, Any], records: list[IngestRecord]) -> int:
    spool_path = storage_paths(config)["spool_path"]
    count = 0
    for record in records:
        append_jsonl(
            spool_path,
            {
                "spooled_at": utc_now_iso(),
                "session": record.session,
                "message": record.message,
            },
        )
        count += 1
    return count


def count_spool_records(config: dict[str, Any]) -> int:
    spool_path = storage_paths(config)["spool_path"]
    if not spool_path.exists():
        return 0
    return sum(1 for line in spool_path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip())


def flush_spool_records(config: dict[str, Any]) -> dict[str, Any]:
    spool_path = storage_paths(config)["spool_path"]
    if not spool_path.exists():
        return {"ok": True, "flushed_messages": 0}
    if not primary_storage_available(config):
        return {"ok": False, "error": "primary_storage_unavailable", "flushed_messages": 0}
    raw_lines = spool_path.read_text(encoding="utf-8", errors="replace").splitlines()
    records: list[IngestRecord] = []
    for raw_line in raw_lines:
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        session = payload.get("session")
        message = payload.get("message")
        if not isinstance(session, dict) or not isinstance(message, dict):
            continue
        records.append(IngestRecord(session=session, message=message))
    local_state = load_local_state(config)
    insert_records_into_db(config, records, local_state)
    spool_path.unlink(missing_ok=True)
    return {"ok": True, "flushed_messages": len(records)}


def select_provider(config: dict[str, Any], requested: str) -> list[str]:
    normalized_requested = str(requested or "auto").strip().lower()
    if normalized_requested not in {"auto", *REVIEW_PROVIDER_ORDER}:
        raise ValueError(f"unsupported_review_provider:{normalized_requested}")
    preferred = [
        str(item).strip().lower()
        for item in (config.get("review", {}) or {}).get("preferred_providers", [])
        if str(item).strip()
    ]
    if normalized_requested != "auto":
        preferred = [normalized_requested]
    preferred = [item for item in preferred if item in REVIEW_PROVIDER_ORDER]
    if not preferred:
        preferred = list(REVIEW_PROVIDER_ORDER)
    capabilities = probe_capabilities(load_registry())
    reviewers = capabilities.get("reviewers", {}) if isinstance(capabilities.get("reviewers"), dict) else {}
    available = [item for item in preferred if bool((reviewers.get(item) or {}).get("available"))]
    return available or preferred


def conversation_review_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "summary",
            "prompt_hygiene",
            "verbosity_waste",
            "instruction_conflicts",
            "ambiguity_sources",
            "workflow_efficiency_suggestions",
            "agent_error_inducing_patterns",
            "notable_positive_patterns",
            "recommended_adjustments",
        ],
        "additionalProperties": True,
        "properties": {
            "summary": {"type": "string"},
            "prompt_hygiene": {"type": "array", "items": {"type": "string"}},
            "verbosity_waste": {"type": "array", "items": {"type": "string"}},
            "instruction_conflicts": {"type": "array", "items": {"type": "string"}},
            "ambiguity_sources": {"type": "array", "items": {"type": "string"}},
            "workflow_efficiency_suggestions": {"type": "array", "items": {"type": "string"}},
            "agent_error_inducing_patterns": {"type": "array", "items": {"type": "string"}},
            "notable_positive_patterns": {"type": "array", "items": {"type": "string"}},
            "recommended_adjustments": {"type": "array", "items": {"type": "string"}},
        },
    }


def normalize_review_payload(raw: dict[str, Any] | None) -> dict[str, Any]:
    payload = raw if isinstance(raw, dict) else {}

    def _list(name: str) -> list[str]:
        value = payload.get(name, [])
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    return {
        "summary": str(payload.get("summary", "")).strip() or "No summary provided.",
        "prompt_hygiene": _list("prompt_hygiene"),
        "verbosity_waste": _list("verbosity_waste"),
        "instruction_conflicts": _list("instruction_conflicts"),
        "ambiguity_sources": _list("ambiguity_sources"),
        "workflow_efficiency_suggestions": _list("workflow_efficiency_suggestions"),
        "agent_error_inducing_patterns": _list("agent_error_inducing_patterns"),
        "notable_positive_patterns": _list("notable_positive_patterns"),
        "recommended_adjustments": _list("recommended_adjustments"),
    }


def review_payload_is_useful(payload: dict[str, Any]) -> bool:
    if str(payload.get("summary", "")).strip() and str(payload.get("summary", "")).strip() != "No summary provided.":
        return True
    for key in (
        "prompt_hygiene",
        "verbosity_waste",
        "instruction_conflicts",
        "ambiguity_sources",
        "workflow_efficiency_suggestions",
        "agent_error_inducing_patterns",
        "notable_positive_patterns",
        "recommended_adjustments",
    ):
        if payload.get(key):
            return True
    return False


def query_review_scope(conn: sqlite3.Connection) -> dict[str, Any]:
    last_review = conn.execute(
        "SELECT completed_at, review_run_id, summary FROM review_runs WHERE status = 'completed' ORDER BY completed_at DESC LIMIT 1"
    ).fetchone()
    if last_review is not None and str(last_review["summary"]).strip() != "No summary provided.":
        since = str(last_review["completed_at"])
    else:
        since = ""
    if since:
        rows = conn.execute(
            """
            SELECT m.ts, m.role, m.speaker, m.body, m.local_date, s.thread_name, s.session_id
            FROM messages m
            JOIN source_sessions s ON s.session_id = m.session_id
            WHERE m.ts > ?
            ORDER BY m.ts ASC
            """,
            (since,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT m.ts, m.role, m.speaker, m.body, m.local_date, s.thread_name, s.session_id
            FROM messages m
            JOIN source_sessions s ON s.session_id = m.session_id
            ORDER BY m.ts ASC
            """
        ).fetchall()
    messages = [dict(row) for row in rows]
    instance_rows = conn.execute(
        "SELECT * FROM archive_instances ORDER BY local_date ASC"
    ).fetchall()
    instances = [dict(row) for row in instance_rows]
    return {"since": since, "messages": messages, "instances": instances}


def review_stats(messages: list[dict[str, Any]]) -> dict[str, Any]:
    user_count = sum(1 for item in messages if str(item.get("role")) == "user")
    assistant_count = sum(1 for item in messages if str(item.get("role")) == "assistant")
    lengths = [len(str(item.get("body", ""))) for item in messages]
    average_length = round(sum(lengths) / len(lengths), 2) if lengths else 0.0
    long_count = sum(1 for length in lengths if length >= 800)
    repeats: dict[str, int] = {}
    for item in messages:
        body = str(item.get("body", "")).strip()
        if not body:
            continue
        key = body[:120]
        repeats[key] = repeats.get(key, 0) + 1
    repeated = sorted(
        [{"snippet": key, "count": count} for key, count in repeats.items() if count > 1],
        key=lambda item: (-int(item["count"]), str(item["snippet"])),
    )[:10]
    return {
        "message_count": len(messages),
        "user_count": user_count,
        "assistant_count": assistant_count,
        "average_length": average_length,
        "long_message_count": long_count,
        "repeated_snippets": repeated,
    }


def build_review_input_file(config: dict[str, Any], scope: dict[str, Any], stats: dict[str, Any]) -> Path:
    review_input_dir = storage_paths(config)["review_input_dir"]
    review_input_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = review_input_dir / f"conversation-review-input-{run_id}.md"
    lines = [
        "# AGN Conversation Archive Review Input",
        "",
        f"- generated_at: {utc_now_iso()}",
        f"- since_last_review: {scope.get('since') or 'full_history'}",
        f"- message_count: {stats['message_count']}",
        f"- user_count: {stats['user_count']}",
        f"- assistant_count: {stats['assistant_count']}",
        f"- average_length: {stats['average_length']}",
        f"- long_message_count: {stats['long_message_count']}",
        "",
        "## Repeated Snippets",
    ]
    for item in stats["repeated_snippets"]:
        lines.append(f"- x{item['count']}: {item['snippet']}")
    lines.extend(["", "## Transcript"])
    for item in scope["messages"]:
        thread_name = str(item.get("thread_name", "")).strip() or str(item.get("session_id", "")).strip()
        lines.append(
            f"{item['ts']}\t{item['role']}\t[{thread_name}]\t{str(item.get('body', '')).replace(chr(10), ' ')}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def build_review_prompt(input_path: Path) -> str:
    schema = json.dumps(conversation_review_schema(), ensure_ascii=True)
    return (
        "You are a flagship reviewer auditing archived operator conversations for prompt hygiene and workflow quality.\n"
        "This review lane is reserved for flagship reasoning models only.\n"
        "Read the conversation archive input file and produce a strict JSON object only.\n"
        "Do not rewrite the conversation. Do not suggest automatic system changes.\n"
        "Focus on user-side prompt quality, ambiguity, waste, contradictions, and work habit improvements.\n"
        "The downstream system will render your structured findings into a Markdown report for the operator.\n"
        "Required output schema:\n"
        f"{schema}\n\n"
        f"Input file:\n{input_path}\n"
    )


def provider_command(provider: str, config: dict[str, Any], prompt: str) -> list[str]:
    review_cfg = config.get("review", {}) if isinstance(config.get("review"), dict) else {}
    if provider == "claude":
        cmd = [
            "claude",
            "-p",
            "--permission-mode",
            "plan",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(conversation_review_schema(), ensure_ascii=True),
            "--add-dir",
            str(ROOT),
        ]
        model = str(review_cfg.get("claude_model", "opus")).strip()
        if model:
            cmd.extend(["--model", model])
        cmd.append(prompt)
        return cmd
    if provider == "gemini":
        cmd = [
            "gemini",
            "-p",
            prompt,
            "--output-format",
            "text",
            "--include-directories",
            str(ROOT),
        ]
        model = str(review_cfg.get("gemini_model", "pro")).strip()
        if model:
            cmd.extend(["--model", model])
        return cmd
    raise ValueError(f"unsupported_review_provider:{provider}")


def run_provider_review(config: dict[str, Any], provider: str, prompt: str) -> dict[str, Any]:
    cmd = provider_command(provider, config, prompt)
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            check=False,
            timeout=900.0,
        )
    except FileNotFoundError as exc:
        return {
            "provider": provider,
            "returncode": 127,
            "stdout": "",
            "stderr": f"provider_executable_missing:{exc}",
            "parsed": None,
        }
    raw_stdout = str(completed.stdout or "").strip()
    parsed = extract_json_object(raw_stdout)
    if isinstance(parsed, dict) and isinstance(parsed.get("structured_output"), dict):
        parsed = parsed.get("structured_output")
    return {
        "provider": provider,
        "returncode": int(completed.returncode),
        "stdout": raw_stdout,
        "stderr": str(completed.stderr or "").strip(),
        "parsed": parsed,
    }


def render_markdown_report(scope: dict[str, Any], stats: dict[str, Any], provider: str, review_payload: dict[str, Any]) -> str:
    reviewer_label = FLAGSHIP_REVIEWER_LABELS.get(provider, provider)
    sections = [
        "# AGN Conversation Archive Review",
        "",
        f"- generated_at: {utc_now_iso()}",
        f"- reviewer_provider: {reviewer_label}",
        f"- since_last_review: {scope.get('since') or 'full_history'}",
        f"- message_count: {stats['message_count']}",
        f"- user_count: {stats['user_count']}",
        f"- assistant_count: {stats['assistant_count']}",
        "",
        "## Summary",
        review_payload["summary"],
        "",
    ]
    section_order = [
        ("Prompt Hygiene", "prompt_hygiene"),
        ("Verbosity Waste", "verbosity_waste"),
        ("Instruction Conflicts", "instruction_conflicts"),
        ("Ambiguity Sources", "ambiguity_sources"),
        ("Workflow Efficiency Suggestions", "workflow_efficiency_suggestions"),
        ("Agent Error Inducing Patterns", "agent_error_inducing_patterns"),
        ("Notable Positive Patterns", "notable_positive_patterns"),
        ("Recommended Adjustments", "recommended_adjustments"),
    ]
    for title, key in section_order:
        sections.append(f"## {title}")
        items = review_payload.get(key, [])
        if items:
            sections.extend([f"- {item}" for item in items])
        else:
            sections.append("- None.")
        sections.append("")
    return "\n".join(sections).rstrip() + "\n"


def store_review_result(
    conn: sqlite3.Connection,
    *,
    review_run_id: str,
    provider: str,
    instance_scope: str,
    report_ref: str,
    review_payload: dict[str, Any],
) -> None:
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO review_runs (
            review_run_id, instance_scope, provider, started_at, completed_at, status, report_ref, summary
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            review_run_id,
            instance_scope,
            provider,
            now,
            now,
            "completed",
            report_ref,
            review_payload["summary"],
        ),
    )
    finding_keys = [
        "prompt_hygiene",
        "verbosity_waste",
        "instruction_conflicts",
        "ambiguity_sources",
        "workflow_efficiency_suggestions",
        "agent_error_inducing_patterns",
        "notable_positive_patterns",
        "recommended_adjustments",
    ]
    for key in finding_keys:
        for idx, item in enumerate(review_payload.get(key, []), start=1):
            conn.execute(
                "INSERT INTO review_findings (review_run_id, finding_type, ordinal, content) VALUES (?, ?, ?, ?)",
                (review_run_id, key, idx, item),
            )
    conn.commit()


def run_review(config: dict[str, Any], *, requested_provider: str = "auto", force: bool = False) -> dict[str, Any]:
    if not primary_storage_available(config):
        return {"ok": False, "error": "primary_storage_unavailable"}
    db_path = storage_paths(config)["primary_db_path"]
    conn = sqlite_connect(db_path)
    ensure_schema(conn)
    scope = query_review_scope(conn)
    if not scope["messages"] and not force:
        conn.close()
        return {"ok": True, "status": "skipped_no_messages"}
    stats = review_stats(scope["messages"])
    input_path = build_review_input_file(config, scope, stats)
    prompt = build_review_prompt(input_path)
    attempted: list[dict[str, Any]] = []
    for provider in select_provider(config, requested_provider):
        result = run_provider_review(config, provider, prompt)
        attempted.append(result)
        if result["returncode"] != 0:
            continue
        parsed = normalize_review_payload(result["parsed"])
        if not review_payload_is_useful(parsed):
            continue
        run_id = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        report_path = storage_paths(config)["review_report_dir"] / f"conversation-review-{run_id}.md"
        report_path.write_text(render_markdown_report(scope, stats, provider, parsed), encoding="utf-8")
        raw_report_path = report_path.with_suffix(".json")
        raw_report_path.write_text(
            json.dumps({"provider_output": result, "normalized_review": parsed, "scope_stats": stats}, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        review_run_id = f"review-{run_id}"
        store_review_result(
            conn,
            review_run_id=review_run_id,
            provider=provider,
            instance_scope=json.dumps({"since": scope.get("since", ""), "message_count": len(scope["messages"])}, ensure_ascii=True),
            report_ref=str(report_path),
            review_payload=parsed,
        )
        conn.close()
        return {
            "ok": True,
            "status": "completed",
            "provider": provider,
            "report_path": str(report_path),
            "raw_report_path": str(raw_report_path),
            "attempted": attempted,
        }
    conn.close()
    return {"ok": False, "status": "failed", "attempted": attempted}


def scan_once(config: dict[str, Any], *, days: int = 0) -> dict[str, Any]:
    ensure_runtime_dirs(config)
    local_state = load_local_state(config)
    files_state = local_state.get("files", {})
    if not isinstance(files_state, dict):
        files_state = {}
    records: list[IngestRecord] = []
    scanned_files = 0
    for codex_home_raw in (config.get("sources", {}) or {}).get("codex_homes", []):
        codex_home = Path(str(codex_home_raw))
        if not codex_home.exists():
            continue
        thread_names = discover_thread_names(codex_home)
        for rollout_path in iter_rollout_files(codex_home, days=days):
            scanned_files += 1
            state_entry = files_state.get(str(rollout_path))
            parsed_records, updated_state = read_rollout_delta(
                path=rollout_path,
                source_home=codex_home,
                thread_names=thread_names,
                state_entry=state_entry if isinstance(state_entry, dict) else None,
            )
            files_state[str(rollout_path)] = updated_state
            records.extend(parsed_records)
    local_state["files"] = files_state
    local_state["last_scan_at"] = utc_now_iso()
    save_local_state(config, local_state)
    if not records:
        return {"ok": True, "scanned_files": scanned_files, "new_messages": 0, "stored_in": "none"}
    if primary_storage_available(config):
        flush_result = flush_spool_records(config)
        store_result = insert_records_into_db(config, records, local_state)
        return {
            "ok": True,
            "scanned_files": scanned_files,
            "new_messages": len(records),
            "stored_in": "primary_db",
            "flush_result": flush_result,
            "store_result": store_result,
        }
    spooled = spool_records(config, records)
    return {
        "ok": True,
        "scanned_files": scanned_files,
        "new_messages": len(records),
        "stored_in": "local_spool",
        "spooled_messages": spooled,
    }


def maybe_run_periodic_review(config: dict[str, Any]) -> dict[str, Any]:
    review_cfg = config.get("review", {}) if isinstance(config.get("review"), dict) else {}
    if not bool(review_cfg.get("enabled", True)):
        return {"ok": True, "status": "disabled"}
    if not primary_storage_available(config):
        return {"ok": False, "status": "primary_storage_unavailable"}
    conn = sqlite_connect(storage_paths(config)["primary_db_path"])
    ensure_schema(conn)
    last = conn.execute(
        "SELECT completed_at FROM review_runs WHERE status = 'completed' ORDER BY completed_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    cadence_hours = max(1, int(review_cfg.get("cadence_hours", 72) or 72))
    if last is not None:
        next_due = parse_iso_ts(str(last["completed_at"])) + timedelta(hours=cadence_hours)
        if datetime.now(tz=timezone.utc) < next_due:
            return {"ok": True, "status": "not_due", "next_due": next_due.isoformat()}
    return run_review(config, requested_provider="auto", force=False)


def build_status(config: dict[str, Any]) -> dict[str, Any]:
    paths = storage_paths(config)
    local_state = load_local_state(config)
    payload = {
        "ok": True,
        "generated_at": utc_now_iso(),
        "scope": config.get("scope", "codex_admin"),
        "primary_db_path": str(paths["primary_db_path"]),
        "primary_storage_available": primary_storage_available(config),
        "spool_path": str(paths["spool_path"]),
        "spool_records": count_spool_records(config),
        "tracked_rollout_files": len((local_state.get("files") or {}) if isinstance(local_state.get("files"), dict) else {}),
        "last_scan_at": str(local_state.get("last_scan_at", "")),
        "review": config.get("review", {}),
    }
    if primary_storage_available(config) and paths["primary_db_path"].exists():
        conn = sqlite_connect(paths["primary_db_path"])
        ensure_schema(conn)
        counts = {
            "source_sessions": int(conn.execute("SELECT COUNT(*) FROM source_sessions").fetchone()[0]),
            "messages": int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]),
            "archive_instances": int(conn.execute("SELECT COUNT(*) FROM archive_instances").fetchone()[0]),
            "review_runs": int(conn.execute("SELECT COUNT(*) FROM review_runs").fetchone()[0]),
        }
        last_review = conn.execute(
            "SELECT provider, completed_at, report_ref FROM review_runs WHERE status = 'completed' ORDER BY completed_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        payload["db_counts"] = counts
        payload["last_review"] = dict(last_review) if last_review is not None else {}
    return payload


def export_day(config: dict[str, Any], day: str) -> dict[str, Any]:
    if not primary_storage_available(config):
        return {"ok": False, "error": "primary_storage_unavailable"}
    conn = sqlite_connect(storage_paths(config)["primary_db_path"])
    ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT m.ts, m.role, m.body, s.thread_name, s.session_id
        FROM messages m
        JOIN source_sessions s ON s.session_id = m.session_id
        WHERE m.local_date = ?
        ORDER BY m.ts ASC
        """,
        (day,),
    ).fetchall()
    conn.close()
    export_path = storage_paths(config)["export_dir"] / f"{day}.md"
    lines = [f"# Conversation Archive Export {day}", ""]
    if not rows:
        lines.append("No messages found.")
    else:
        for row in rows:
            thread_name = str(row["thread_name"]).strip() or str(row["session_id"]).strip()
            lines.append(f"{row['ts']}\t{row['role']}\t[{thread_name}]\t{str(row['body']).replace(chr(10), ' ')}")
    export_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"ok": True, "export_path": str(export_path), "message_count": len(rows)}


def cmd_backfill(args: argparse.Namespace) -> int:
    config = load_config()
    result = scan_once(config, days=max(0, int(args.days or 0)))
    audit(config, "conversation_archive_backfill", **result)
    update_status(config, build_status(config))
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result.get("ok") else 1


def cmd_flush_spool(_args: argparse.Namespace) -> int:
    config = load_config()
    result = flush_spool_records(config)
    audit(config, "conversation_archive_flush_spool", **result)
    update_status(config, build_status(config))
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result.get("ok") else 1


def cmd_review_now(args: argparse.Namespace) -> int:
    config = load_config()
    requested = str(args.provider or "auto").strip().lower()
    if requested not in {"auto", "claude", "gemini"}:
        payload = {"ok": False, "error": f"unsupported_review_provider:{requested}"}
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 1
    result = run_review(config, requested_provider=requested, force=True)
    audit(config, "conversation_archive_review_now", **result)
    update_status(config, build_status(config))
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result.get("ok") else 1


def cmd_status(_args: argparse.Namespace) -> int:
    config = load_config()
    payload = build_status(config)
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def cmd_export_day(args: argparse.Namespace) -> int:
    config = load_config()
    result = export_day(config, str(args.day))
    audit(config, "conversation_archive_export_day", **result)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result.get("ok") else 1


def run_loop(config: dict[str, Any]) -> int:
    interval = max(5.0, float(config.get("poll_interval_seconds", 30.0) or 30.0))
    should_stop = False

    def _stop(_signum, _frame) -> None:
        nonlocal should_stop
        should_stop = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    audit(config, "conversation_archive_loop_started", interval_seconds=interval)
    while not should_stop:
        scan_result = scan_once(config)
        try:
            review_result = maybe_run_periodic_review(config)
        except Exception as exc:
            review_result = {"ok": False, "status": "error", "error": f"{type(exc).__name__}:{exc}"}
            audit(config, "conversation_archive_periodic_review_error", error=str(review_result["error"]))
        payload = build_status(config)
        payload["last_scan_result"] = scan_result
        payload["last_periodic_review_result"] = review_result
        update_status(config, payload)
        audit(
            config,
            "conversation_archive_loop_tick",
            scan_result=scan_result,
            review_result=review_result,
        )
        time.sleep(interval)
    audit(config, "conversation_archive_loop_stopped")
    update_status(config, build_status(config))
    return 0


def cmd_run_loop(_args: argparse.Namespace) -> int:
    config = load_config()
    return run_loop(config)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only AGN Codex/Admin conversation archive and review daemon")
    sub = parser.add_subparsers(dest="command", required=True)

    backfill_parser = sub.add_parser("backfill", help="Backfill historical Codex/Admin conversations")
    backfill_parser.add_argument("--days", type=int, default=0)
    backfill_parser.set_defaults(func=cmd_backfill)

    run_loop_parser = sub.add_parser("run-loop", help="Run the background conversation archive loop")
    run_loop_parser.set_defaults(func=cmd_run_loop)

    flush_parser = sub.add_parser("flush-spool", help="Flush local spool records into the primary archive database")
    flush_parser.set_defaults(func=cmd_flush_spool)

    review_parser = sub.add_parser("review-now", help="Run a manual flagship review across archived conversations")
    review_parser.add_argument("--provider", default="auto")
    review_parser.set_defaults(func=cmd_review_now)

    status_parser = sub.add_parser("status", help="Show archive status")
    status_parser.set_defaults(func=cmd_status)

    export_parser = sub.add_parser("export-day", help="Export one archived local day to markdown")
    export_parser.add_argument("day")
    export_parser.set_defaults(func=cmd_export_day)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
