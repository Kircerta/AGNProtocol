"""AGN dispatch event store and event-driven SSOT helpers.

This is the real package implementation for AGN's append-only event log,
checkpoint state, action/control queues, and integrity helpers.
The legacy script remains only as a compatibility shim.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from typing import Any

try:
    import fcntl  # POSIX advisory file locks (cross-process sequence safety)
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from agn_refs import build_repo_ref, parse_repo_ref
from pointer_protocol import resolve_ref_path


PACKAGE_PATH = "agn.dispatch.event_store"
LEGACY_SCRIPT_SHIM = "scripts/event_sourcing.py"
EVENT_ROOT = ROOT / ".agn_workspace" / "event_driven"
SSOT_ROOT = EVENT_ROOT / "ssot"
EVENTS_DIR = SSOT_ROOT / "events"
CHECKPOINT_DIR = SSOT_ROOT / "checkpoints"
MANIFEST_DIR = SSOT_ROOT / "manifests"
PERF_DIR = SSOT_ROOT / "perf"
SNAPSHOT_DIR = SSOT_ROOT / "snapshots"
ACTIONS_DIR = EVENT_ROOT / "actions"
ACTIONS_PENDING_DIR = ACTIONS_DIR / "pending"
ACTIONS_DONE_DIR = ACTIONS_DIR / "done"
ACTIONS_FAILED_DIR = ACTIONS_DIR / "failed"
CONTROL_DIR = EVENT_ROOT / "control"
CONTROL_PENDING_DIR = CONTROL_DIR / "pending"
CONTROL_DONE_DIR = CONTROL_DIR / "done"
CONTROL_FAILED_DIR = CONTROL_DIR / "failed"
SCRATCH_DIR = EVENT_ROOT / "scratch"
REPO_MAP_PATH = SSOT_ROOT / "repo_refs.json"

CONTROL_TYPES = {
    "PAUSE",
    "STOP",
    "MODIFY",
    "RESUME",
    "STATUS",
    "DEGRADE",
    "REORGANIZE",
    "FALLBACK_TOPIC",
    "MARK_ANOMALY",
}

STATES = {
    "CREATED",
    "PLANNED",
    "DISPATCHED_EXEC",
    "EXEC_RUNNING",
    "EXEC_DONE",
    "DISPATCHED_REVIEW",
    "REVIEW_RUNNING",
    "REVIEW_DONE",
    "SYNTHESIS",
    "DELIVERY_GATE",
    "NEED_ADMIN",
    "DELIVERED",
    "ABORTED",
}

VALID_TRANSITIONS: dict[str, set[str]] = {
    "CREATED": {"PLANNED", "ABORTED"},
    "PLANNED": {"DISPATCHED_EXEC", "ABORTED", "NEED_ADMIN"},
    "DISPATCHED_EXEC": {"EXEC_RUNNING", "PLANNED", "ABORTED", "NEED_ADMIN"},
    "EXEC_RUNNING": {"EXEC_DONE", "DISPATCHED_EXEC", "PLANNED", "ABORTED", "NEED_ADMIN"},
    "EXEC_DONE": {"DISPATCHED_REVIEW", "SYNTHESIS", "PLANNED", "ABORTED", "NEED_ADMIN"},
    "DISPATCHED_REVIEW": {"REVIEW_RUNNING", "PLANNED", "ABORTED", "NEED_ADMIN"},
    "REVIEW_RUNNING": {"REVIEW_DONE", "DISPATCHED_REVIEW", "PLANNED", "ABORTED", "NEED_ADMIN"},
    "REVIEW_DONE": {"SYNTHESIS", "DELIVERY_GATE", "PLANNED", "ABORTED", "NEED_ADMIN"},
    "SYNTHESIS": {"DISPATCHED_REVIEW", "REVIEW_RUNNING", "REVIEW_DONE", "DELIVERY_GATE", "PLANNED", "ABORTED", "NEED_ADMIN"},
    "DELIVERY_GATE": {"DELIVERED", "PLANNED", "DISPATCHED_REVIEW", "ABORTED", "NEED_ADMIN"},
    "NEED_ADMIN": {"PLANNED", "ABORTED"},
    "DELIVERED": set(),
    "ABORTED": set(),
}

_SEQ_LOCKS: dict[str, threading.Lock] = {}
_SEQ_LOCKS_GUARD = threading.Lock()


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def ensure_event_dirs() -> None:
    for path in (
        EVENTS_DIR,
        CHECKPOINT_DIR,
        MANIFEST_DIR,
        PERF_DIR,
        SNAPSHOT_DIR,
        ACTIONS_PENDING_DIR,
        ACTIONS_DONE_DIR,
        ACTIONS_FAILED_DIR,
        CONTROL_PENDING_DIR,
        CONTROL_DONE_DIR,
        CONTROL_FAILED_DIR,
        SCRATCH_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)


def _load_repo_map() -> dict[str, str]:
    if not REPO_MAP_PATH.exists():
        return {}
    try:
        payload = json.loads(REPO_MAP_PATH.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not key.startswith("agn://repo/"):
            continue
        if not isinstance(value, str) or not value.strip():
            continue
        out[key] = value
    return out


def _save_repo_map(payload: dict[str, str]) -> None:
    ensure_event_dirs()
    _atomic_write_json(REPO_MAP_PATH, payload)


def register_repo_ref(*, repo_ref: str, repo_path: str) -> None:
    clean_ref = str(repo_ref or "").strip()
    clean_path = str(repo_path or "").strip()
    if not clean_ref or not clean_path:
        return
    parse_repo_ref(clean_ref)
    resolved = str(Path(clean_path).expanduser().resolve())
    current = _load_repo_map()
    if current.get(clean_ref) == resolved:
        return
    current[clean_ref] = resolved
    _save_repo_map(current)


def resolve_repo_ref(repo_ref: str) -> Path:
    clean_ref = str(repo_ref or "").strip()
    if not clean_ref:
        return ROOT
    parse_repo_ref(clean_ref)
    current = _load_repo_map()
    mapped = str(current.get(clean_ref, "")).strip()
    if mapped:
        resolved = Path(mapped).expanduser().resolve()
        if resolved.exists() and resolved.is_dir():
            return resolved
        if clean_ref == build_repo_ref("main"):
            current[clean_ref] = str(ROOT)
            _save_repo_map(current)
            return ROOT
        raise ValueError(f"repo_ref_stale_mapping:{clean_ref}")
    if clean_ref == build_repo_ref("main"):
        return ROOT
    raise ValueError(f"repo_ref_not_registered:{clean_ref}")


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            if not content.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))


def _events_path(trace_id: str) -> Path:
    safe = str(trace_id or "").strip().replace("/", "_").replace("..", "_")
    return EVENTS_DIR / f"{safe}.jsonl"


def _seq_path(trace_id: str) -> Path:
    safe = str(trace_id or "").strip().replace("/", "_").replace("..", "_")
    return EVENTS_DIR / f"{safe}.seq"


def _seq_lock(trace_id: str) -> threading.Lock:
    safe = str(trace_id or "").strip().replace("/", "_").replace("..", "_")
    with _SEQ_LOCKS_GUARD:
        lock = _SEQ_LOCKS.get(safe)
        if lock is None:
            lock = threading.Lock()
            _SEQ_LOCKS[safe] = lock
        return lock


def _checkpoint_path(task_id: str) -> Path:
    safe = str(task_id or "").strip().replace("/", "_").replace("..", "_")
    return CHECKPOINT_DIR / f"{safe}.json"


def _next_event_id(trace_id: str) -> str:
    path = _seq_path(trace_id)
    with _seq_lock(trace_id):
        path.parent.mkdir(parents=True, exist_ok=True)
        current = 0
        with path.open("a+", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                raw = handle.read().strip()
                try:
                    current = int(raw or "0")
                except Exception:
                    current = 0
                current += 1
                handle.seek(0)
                handle.truncate()
                handle.write(str(current))
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return f"{trace_id}-evt-{current:08d}"


def append_event(
    *,
    trace_id: str,
    task_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    action_id: str = "",
    severity: str = "info",
) -> dict[str, Any]:
    ensure_event_dirs()
    event = {
        "event_id": _next_event_id(trace_id),
        "trace_id": str(trace_id).strip(),
        "task_id": str(task_id).strip(),
        "event_type": str(event_type).strip(),
        "severity": str(severity).strip() or "info",
        "action_id": str(action_id).strip(),
        "ts": utc_now_iso(),
        "payload": payload or {},
    }
    line = json.dumps(event, ensure_ascii=True) + "\n"
    events_path = _events_path(trace_id)
    with events_path.open("a", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return event


def load_events(trace_id: str) -> list[dict[str, Any]]:
    path = _events_path(trace_id)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                loaded = json.loads(line)
            except Exception:
                continue
            if isinstance(loaded, dict):
                events.append(loaded)
    return events


def load_checkpoint(task_id: str) -> dict[str, Any] | None:
    path = _checkpoint_path(task_id)
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return loaded if isinstance(loaded, dict) else None


def write_checkpoint(task_id: str, payload: dict[str, Any]) -> Path:
    path = _checkpoint_path(task_id)
    data = dict(payload)
    data["task_id"] = str(task_id).strip()
    data["updated_at"] = utc_now_iso()
    _atomic_write_json(path, data)
    return path


def transition_state(
    *,
    trace_id: str,
    task_id: str,
    to_state: str,
    reason: str,
) -> tuple[bool, str]:
    target = str(to_state or "").strip().upper()
    if target not in STATES:
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="PROTOCOL_VIOLATION",
            payload={"reason": f"unknown target state: {to_state}"},
            severity="error",
        )
        return False, "unknown_state"

    checkpoint = load_checkpoint(task_id) or {}
    current = str(checkpoint.get("state", "CREATED")).strip().upper() or "CREATED"
    allowed = VALID_TRANSITIONS.get(current, set())
    if target not in allowed and target != current:
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="PROTOCOL_VIOLATION",
            payload={"reason": f"invalid transition {current}->{target}", "from": current, "to": target},
            severity="error",
        )
        return False, "invalid_transition"

    append_event(
        trace_id=trace_id,
        task_id=task_id,
        event_type="STATE_TRANSITION",
        payload={"from": current, "to": target, "reason": reason},
    )
    write_checkpoint(
        task_id,
        {
            **checkpoint,
            "trace_id": trace_id,
            "state": target,
            "state_reason": reason,
            "last_event_time": utc_now_iso(),
        },
    )
    return True, ""


def enqueue_action(action_payload: dict[str, Any]) -> Path:
    ensure_event_dirs()
    action_id = str(action_payload.get("action_id", "")).strip() or "unnamed-action"
    trace_id = str(action_payload.get("trace_id", "")).strip() or "trace"
    filename = f"{trace_id}__{action_id}.json"
    target = ACTIONS_PENDING_DIR / filename
    _atomic_write_json(target, action_payload)
    return target


def list_pending_actions(*, task_id: str = "", trace_id: str = "") -> list[Path]:
    ensure_event_dirs()
    items = sorted(ACTIONS_PENDING_DIR.glob("*.json"))
    if not task_id and not trace_id:
        return items
    matched: list[Path] = []
    target_task = str(task_id).strip()
    target_trace = str(trace_id).strip()
    for path in items:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if target_task and str(payload.get("task_id", "")).strip() != target_task:
            continue
        if target_trace and str(payload.get("trace_id", "")).strip() != target_trace:
            continue
        matched.append(path)
    return matched


def move_action_file(path: Path, *, status: str) -> Path:
    ensure_event_dirs()
    destination_root = ACTIONS_DONE_DIR if status == "done" else ACTIONS_FAILED_DIR
    destination = destination_root / path.name
    if not path.exists():
        alt_done = ACTIONS_DONE_DIR / path.name
        if alt_done.exists():
            return alt_done
        alt_failed = ACTIONS_FAILED_DIR / path.name
        if alt_failed.exists():
            return alt_failed
        return destination
    try:
        path.replace(destination)
    except FileNotFoundError:
        alt_done = ACTIONS_DONE_DIR / path.name
        if alt_done.exists():
            return alt_done
        alt_failed = ACTIONS_FAILED_DIR / path.name
        if alt_failed.exists():
            return alt_failed
    return destination


def cancel_pending_actions(*, trace_id: str, task_id: str, reason: str) -> list[Path]:
    cancelled: list[Path] = []
    for path in list_pending_actions(task_id=task_id, trace_id=trace_id):
        action_id = path.stem.split("__", 1)[-1]
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="ACTION_CANCELLED",
            action_id=action_id,
            payload={"reason": str(reason).strip() or "control_preemption"},
            severity="warn",
        )
        cancelled.append(move_action_file(path, status="failed"))
    return cancelled


def heartbeat_tick(*, trace_id: str, task_id: str, note: str = "") -> dict[str, Any]:
    event = append_event(
        trace_id=trace_id,
        task_id=task_id,
        event_type="HEARTBEAT_TICK",
        payload={"note": note or "periodic wake", "ts": utc_now_iso()},
    )
    checkpoint = load_checkpoint(task_id) or {"trace_id": trace_id, "state": "CREATED"}
    checkpoint["trace_id"] = trace_id
    checkpoint["last_event_time"] = event["ts"]
    write_checkpoint(task_id, checkpoint)
    return event


def _control_filename(control_type: str, control_id: str, task_id: str) -> str:
    kind = str(control_type or "").strip().upper()
    cid = str(control_id or "").strip().replace("/", "_") or "control"
    tid = str(task_id or "all").strip().replace("/", "_")
    return f"{kind}__{tid}__{cid}.json"


def enqueue_control_command(payload: dict[str, Any]) -> Path:
    ensure_event_dirs()
    control_type = str(payload.get("control_type", "")).strip().upper()
    if control_type not in CONTROL_TYPES:
        raise ValueError(f"unsupported control_type: {control_type}")
    control_id = str(payload.get("control_id", "")).strip() or f"ctl-{_next_event_id('control')}"
    task_id = str(payload.get("task_id", "")).strip()
    enriched = dict(payload)
    enriched["control_type"] = control_type
    enriched["control_id"] = control_id
    if "created_at" not in enriched:
        enriched["created_at"] = utc_now_iso()
    name = _control_filename(control_type, control_id, task_id)
    target = CONTROL_PENDING_DIR / name
    _atomic_write_json(target, enriched)
    return target


def list_pending_control_commands(*, task_id: str = "") -> list[Path]:
    ensure_event_dirs()
    items = sorted(CONTROL_PENDING_DIR.glob("*.json"))
    target = str(task_id).strip()
    if not target:
        return items
    matched: list[Path] = []
    for path in items:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        bound_task = str(payload.get("task_id", "")).strip()
        if bound_task and bound_task != target:
            continue
        matched.append(path)
    return matched


def move_control_file(path: Path, *, status: str) -> Path:
    ensure_event_dirs()
    destination_root = CONTROL_DONE_DIR if status == "done" else CONTROL_FAILED_DIR
    destination = destination_root / path.name
    if not path.exists():
        alt_done = CONTROL_DONE_DIR / path.name
        if alt_done.exists():
            return alt_done
        alt_failed = CONTROL_FAILED_DIR / path.name
        if alt_failed.exists():
            return alt_failed
        return destination
    try:
        path.replace(destination)
    except FileNotFoundError:
        alt_done = CONTROL_DONE_DIR / path.name
        if alt_done.exists():
            return alt_done
        alt_failed = CONTROL_FAILED_DIR / path.name
        if alt_failed.exists():
            return alt_failed
    return destination


def load_control_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def watchdog_scan(*, timeout_sec: int = 300) -> list[dict[str, Any]]:
    ensure_event_dirs()
    now = datetime.now(tz=timezone.utc)
    triggered: list[dict[str, Any]] = []
    for cp in sorted(CHECKPOINT_DIR.glob("*.json")):
        checkpoint = load_checkpoint(cp.stem) or {}
        state = str(checkpoint.get("state", "")).strip().upper()
        if state not in {"EXEC_RUNNING", "REVIEW_RUNNING"}:
            continue
        trace_id = str(checkpoint.get("trace_id", "")).strip()
        task_id = str(checkpoint.get("task_id", cp.stem)).strip()
        raw_time = str(checkpoint.get("last_event_time", "")).strip()
        try:
            last = datetime.fromisoformat(raw_time)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        elapsed = (now - last).total_seconds()
        if elapsed < max(1, int(timeout_sec)):
            continue
        already = str(checkpoint.get("watchdog_notified_at", "")).strip()
        if already:
            continue
        timeout_event = append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="TIMEOUT_NO_OUTPUT",
            payload={
                "role": "executor" if state == "EXEC_RUNNING" else "reviewer",
                "last_state": state,
                "elapsed_sec": int(elapsed),
                "hint": "emit RETRY or DEGRADE action",
            },
            severity="warn",
        )
        try:
            from action_protocol import build_action

            recovery_action = build_action(
                trace_id=trace_id,
                task_id=task_id,
                action_id=f"recovery-{state.lower()}-{str(timeout_event.get('event_id', 'evt')).split('-')[-1]}",
                action_type="RETRY",
                inputs={"reason": "timeout_no_output", "last_state": state},
                refs={},
                budget={"max_time_sec": 300, "max_disk_mb": 128, "max_log_kb": 256},
                source_role="coordinator",
                state_hint=state,
            )
            enqueue_action(recovery_action)
        except Exception:
            recovery_action = None
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="WATCHDOG_RECOVERY_PLANNED",
            payload={
                "recovery_action": "RETRY",
                "reason": "timeout_no_output",
                "action_id": (recovery_action or {}).get("action_id", ""),
            },
            severity="warn",
        )
        checkpoint["watchdog_notified_at"] = timeout_event["ts"]
        checkpoint["last_event_time"] = timeout_event["ts"]
        write_checkpoint(task_id, checkpoint)
        triggered.append(timeout_event)
    return triggered


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _collect_refs(value: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(value, str):
        if value.startswith("agn://"):
            refs.append(value)
        return refs
    if isinstance(value, list):
        for item in value:
            refs.extend(_collect_refs(item))
        return refs
    if isinstance(value, dict):
        for item in value.values():
            refs.extend(_collect_refs(item))
        return refs
    return refs


def _rel_path(path: Path) -> str:
    resolved = path.resolve()
    for base in (ROOT.resolve(), EVENT_ROOT.resolve()):
        try:
            return str(resolved.relative_to(base))
        except Exception:
            continue
    try:
        if resolved.is_absolute() and len(resolved.parts) > 1:
            return str(Path(*resolved.parts[1:]))
        return str(resolved)
    except Exception:
        return str(path)


def recent_event_digests(*, trace_id: str, limit: int = 20, payload_preview_chars: int = 240) -> list[dict[str, Any]]:
    events = load_events(trace_id)
    selected = events[-max(1, int(limit)) :]
    digests: list[dict[str, Any]] = []
    max_chars = max(64, int(payload_preview_chars))
    for item in selected:
        payload = item.get("payload", {})
        preview = json.dumps(payload, ensure_ascii=True) if isinstance(payload, (dict, list)) else str(payload)
        if len(preview) > max_chars:
            preview = preview[: max_chars - 24] + "...<truncated-preview>..."
        digests.append(
            {
                "event_id": str(item.get("event_id", "")),
                "event_type": str(item.get("event_type", "")),
                "action_id": str(item.get("action_id", "")),
                "ts": str(item.get("ts", "")),
                "severity": str(item.get("severity", "info")),
                "refs": sorted(set(_collect_refs(payload))),
                "payload_preview": preview,
            }
        )
    return digests


def write_state_snapshot(
    *,
    trace_id: str,
    task_id: str,
    snapshot: dict[str, Any],
    snapshot_ref: str = "",
) -> Path:
    ensure_event_dirs()
    target = SNAPSHOT_DIR / f"{str(trace_id).strip().replace('/', '_')}.snapshot.json"
    rendered = json.dumps(snapshot, ensure_ascii=True, indent=2, sort_keys=True)
    _atomic_write_text(target, rendered)
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    append_event(
        trace_id=trace_id,
        task_id=task_id,
        event_type="STATE_SNAPSHOT_CREATED",
        payload={
            "snapshot_sha256": digest,
            "snapshot_object_ref": f"agn://object/snapshot/{str(trace_id).strip()}/1",
            "snapshot_bytes": len(rendered.encode("utf-8")),
            "snapshot_ref": str(snapshot_ref).strip(),
        },
    )
    return target


def write_manifest(trace_id: str) -> Path:
    ensure_event_dirs()
    events_path = _events_path(trace_id)
    events_hash = _sha256_file(events_path) if events_path.exists() else ""
    refs: list[str] = []
    for event in load_events(trace_id):
        refs.extend(_collect_refs(event.get("payload")))
    unique_refs = sorted(set(refs))
    manifest = {
        "trace_id": trace_id,
        "generated_at": utc_now_iso(),
        "events_ref": f"agn://object/events/{str(trace_id).strip()}/1",
        "events_sha256": events_hash,
        "artifact_refs": unique_refs,
    }
    target = MANIFEST_DIR / f"{trace_id}.manifest.json"
    _atomic_write_json(target, manifest)
    return target


def integrity_check(trace_id: str) -> dict[str, Any]:
    manifest_path = write_manifest(trace_id)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    refs = manifest.get("artifact_refs", [])
    missing: list[dict[str, str]] = []
    for ref in refs:
        try:
            resolve_ref_path(str(ref))
        except Exception as exc:
            missing.append({"ref": str(ref), "error": f"{type(exc).__name__}:{exc}"})
    if missing:
        events = load_events(trace_id)
        task_id = str(events[-1].get("task_id", "")) if events else ""
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="INTEGRITY_ALERT",
            payload={"missing_refs": missing, "reason": "artifact_not_replayable"},
            severity="error",
        )
    return {
        "ok": len(missing) == 0,
        "trace_id": trace_id,
        "manifest": _rel_path(manifest_path),
        "missing_refs": missing,
    }


def _dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def _parse_event_ts(raw: str) -> datetime | None:
    """Parse an ISO timestamp, handling the Z suffix for Python < 3.11."""
    value = str(raw or "").strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        ts = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def load_recent_events(
    *,
    event_type: str | None = None,
    severity: str | None = None,
    max_age_seconds: int = 86400,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Cross-trace event query: scan all trace event files.

    Args:
        event_type: Filter by event_type (exact match). None means all.
        severity: Filter by severity (exact match). None means all.
        max_age_seconds: Only return events younger than this many seconds.
            Defaults to 86400 (24 hours). Set to 0 to disable age filtering.
        limit: Maximum number of matching events to return (newest first).

    Returns:
        List of matching event dicts, sorted newest-first, capped at *limit*.
    """
    ensure_event_dirs()
    cutoff: datetime | None = None
    if max_age_seconds > 0:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(seconds=max_age_seconds)

    # Use file mtime to skip trace files that are certainly older than cutoff.
    cutoff_epoch: float | None = cutoff.timestamp() if cutoff is not None else None

    matched: list[dict[str, Any]] = []
    # Heuristic cap: once we collect limit*3 matches, stop scanning —
    # the final sort will trim to *limit* and the surplus provides margin.
    match_cap = max(1, limit) * 3

    for path in sorted(EVENTS_DIR.glob("*.jsonl"), reverse=True):
        # Skip files whose last modification is older than the cutoff.
        if cutoff_epoch is not None:
            try:
                if path.stat().st_mtime < cutoff_epoch:
                    continue
            except OSError:
                pass  # stat failure — scan the file anyway
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    loaded = json.loads(line)
                except Exception:
                    continue
                if not isinstance(loaded, dict):
                    continue
                if event_type and str(loaded.get("event_type", "")).strip() != event_type:
                    continue
                if severity and str(loaded.get("severity", "")).strip() != severity:
                    continue
                if cutoff is not None:
                    ts = _parse_event_ts(loaded.get("ts", ""))
                    if ts is not None and ts < cutoff:
                        continue
                    # Events with missing or unparseable timestamps pass through
                    # (conservative: avoid data loss).
                matched.append(loaded)
        if len(matched) >= match_cap:
            break

    # Sort newest-first by timestamp
    matched.sort(key=lambda e: str(e.get("ts", "")), reverse=True)
    return matched[:max(1, limit)]


def write_perf_summary(
    *,
    trace_id: str,
    task_id: str,
    started_ts: float,
    actions: list[dict[str, Any]],
    budget: dict[str, float] | None = None,
) -> Path:
    ensure_event_dirs()
    wall_time_sec = max(0.0, __import__("time").time() - started_ts)
    disk_bytes = _dir_size_bytes(EVENT_ROOT)
    logs_bytes = _dir_size_bytes(ROOT / "reports")
    slow_actions = sorted(actions, key=lambda a: float(a.get("duration_ms", 0.0)), reverse=True)[:5]
    payload = {
        "trace_id": trace_id,
        "task_id": task_id,
        "generated_at": utc_now_iso(),
        "wall_time_sec": round(wall_time_sec, 3),
        "event_driven_disk_bytes": disk_bytes,
        "reports_disk_bytes": logs_bytes,
        "action_count": len(actions),
        "top_slow_actions": slow_actions,
    }
    target = PERF_DIR / f"{trace_id}.perf_summary.json"
    _atomic_write_json(target, payload)

    limits = budget or {}
    exceeded: list[str] = []
    max_time = float(limits.get("max_time_sec", 0.0) or 0.0)
    max_disk_mb = float(limits.get("max_disk_mb", 0.0) or 0.0)
    max_log_kb = float(limits.get("max_log_kb", 0.0) or 0.0)
    if max_time > 0 and wall_time_sec > max_time:
        exceeded.append("max_time_sec")
    if max_disk_mb > 0 and disk_bytes > max_disk_mb * 1024 * 1024:
        exceeded.append("max_disk_mb")
    if max_log_kb > 0 and logs_bytes > max_log_kb * 1024:
        exceeded.append("max_log_kb")
    if exceeded:
        append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="PERF_BUDGET_EXCEEDED",
            payload={
                "exceeded": exceeded,
                "wall_time_sec": round(wall_time_sec, 3),
                "event_driven_disk_bytes": disk_bytes,
                "reports_disk_bytes": logs_bytes,
            },
            severity="warn",
        )
    return target
