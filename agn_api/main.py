from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any
import ipaddress
from uuid import uuid4

import anyio
from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from agn_api.audit import AuditLogger
from agn_api.auth import AuthError, decode_bearer_token
from agn_api.config import AppConfig, load_config
from agn_api.sse import SSEHub
from agn_api.ssot_store import SSOTStore
from agn_api.task_engine import derive_status

try:
    from action_protocol import validate_action_payload
    from agn_refs import parse_object_ref
    import event_sourcing as es
    from pointer_protocol import resolve_ref_path
except ImportError:  # pragma: no cover - package import fallback
    from scripts.action_protocol import validate_action_payload
    from scripts.agn_refs import parse_object_ref
    import scripts.event_sourcing as es
    from scripts.pointer_protocol import resolve_ref_path


class PhaseAService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.store = SSOTStore(config.ssot_dir)
        self.audit = AuditLogger(config.audit_log_path)
        self.sse = SSEHub()
        self.executor: ThreadPoolExecutor | None = None



def _task_to_response(task: dict[str, Any]) -> dict[str, Any]:
    payload = dict(task)
    payload["status"] = derive_status(task)
    return payload



def _static_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "static"



def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()



def _sanitize_for_task_id(value: str) -> str:
    """Sanitize an external identifier for use in a task ID.

    Replaces path-unsafe characters and strips leading dots to prevent
    traversal, consistent with ssot_store._safe_id() and pointer_protocol.safe_task_id().
    """
    raw = value.replace("/", "-").replace(" ", "-").lstrip(".")
    if not raw:
        raw = "unnamed"
    if len(raw) > 200:
        raw = raw[:200]
    return raw



# P3-22: reject webhook payloads larger than 1 MB to prevent DoS.
_MAX_WEBHOOK_BODY_BYTES = 1 * 1024 * 1024


def _parse_json_bytes(raw_body: bytes, *, max_bytes: int = _MAX_WEBHOOK_BODY_BYTES) -> dict[str, Any]:
    if max_bytes > 0 and len(raw_body) > max_bytes:
        raise ValueError("payload_too_large")
    try:
        parsed = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_json") from exc

    if not isinstance(parsed, dict):
        raise ValueError("json_must_be_object")

    return parsed


async def _audit_log_async(
    service: PhaseAService,
    *,
    route: str,
    status: int,
    task_id: str | None,
    **extra: object,
) -> None:
    await anyio.to_thread.run_sync(
        lambda: service.audit.log_event(route=route, status=status, task_id=task_id, **extra)
    )


async def _store_get_task_async(service: PhaseAService, task_id: str) -> dict[str, Any] | None:
    return await anyio.to_thread.run_sync(service.store.get_task, task_id)


async def _store_save_task_async(service: PhaseAService, task: dict[str, Any]) -> None:
    await anyio.to_thread.run_sync(lambda: service.store.save_task(task))



def _github_signature_valid(secret: str | None, signature_header: str | None, raw_body: bytes) -> tuple[bool, str]:
    if not secret:
        return False, "secret_not_configured"
    if not signature_header:
        return False, "missing_signature"

    candidate = signature_header.strip()
    if not candidate.startswith("sha256="):
        return False, "invalid_signature_format"

    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"
    if not hmac.compare_digest(candidate, expected):
        return False, "signature_mismatch"

    return True, "ok"



async def _broadcast_task_update(
    *,
    service: PhaseAService,
    task_id: str,
    status_value: str,
    correlation_id: str,
    source: str,
    server_ts_utc: str,
    decision: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "correlation_id": correlation_id,
        "task_id": task_id,
        "status": status_value,
        "source": source,
        "server_ts_utc": server_ts_utc,
    }
    if decision is not None:
        payload["decision"] = decision

    event = await service.sse.broadcast(payload)
    await _audit_log_async(
        service,
        route="/api/events",
        status=200,
        task_id=task_id,
        action="broadcast",
        event_type="sse_broadcast",
        source=source,
        correlation_id=correlation_id,
        event_id=event.get("event_id"),
        decision=decision,
        client_count=await service.sse.client_count(),
    )



def _webhook_task_id(prefix: str, event_id: str) -> str:
    return f"{prefix}-{_sanitize_for_task_id(event_id)}"



_LOCAL_TEST_HOSTS = {"testclient"}
_ALLOWED_CONTROL_TYPES = {
    "PAUSE",
    "RESUME",
    "STOP",
    "STATUS",
    "MODIFY",
    "DEGRADE",
    "REORGANIZE",
    "FALLBACK_TOPIC",
    "MARK_ANOMALY",
}
_ALLOWED_MODIFY_FIELDS = {
    "request_text",
    "request_summary",
    "request_text_ref",
    "acceptance_criteria",
    "needs_context_read",
    "context_read_path",
}
_TIMELINE_EVENT_TYPES = {
    "STATE_TRANSITION",
    "ACTION_STARTED",
    "ACTION_FINISHED",
    "DELIVERY_GATE_PASS",
    "DELIVERY_GATE_FAIL",
    "TIMEOUT_NO_OUTPUT",
    "CONTROL_APPLIED",
    "CONTROL_REJECTED",
    "RESEARCH_SURVEY_CREATED",
    "RESEARCH_SHORTLIST_CREATED",
    "RESEARCH_MESSAGE",
    "RESEARCH_ROUND_REJECTED",
    "RESEARCH_ROUND_APPROVED",
    "RESEARCH_FORCED_DECISION",
    "RESEARCH_EXPERIMENT_FAILED",
    "RESEARCH_DEGRADE_APPLIED",
    "RESEARCH_FINAL_REVIEW",
    "RESEARCH_ARCHIVED",
}
_DEFAULT_REF_READ_MAX_BYTES = 16384
_MAX_REF_READ_BYTES = 65536


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _request_client_host(request: Request) -> str:
    if request.client is None:
        return ""
    return str(request.client.host or "").strip().lower()


def _is_local_client_host(host: str) -> bool:
    value = str(host or "").strip().lower()
    if not value:
        return False
    if value in {"127.0.0.1", "::1", "localhost"}:
        return True
    if value in _LOCAL_TEST_HOSTS:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _parse_iso(ts: str) -> datetime | None:
    raw = str(ts or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _safe_load_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _trace_id_for_projection(task: dict[str, Any], checkpoint: dict[str, Any] | None = None) -> str:
    task_id = str(task.get("id", "")).strip()
    corr = str(task.get("correlation_id", "")).strip()
    if corr:
        return corr.replace("/", "_")
    checkpoint_trace = str((checkpoint or {}).get("trace_id", "")).strip()
    if checkpoint_trace:
        return checkpoint_trace
    return f"trace-{task_id}"


def _task_projection(task: dict[str, Any], checkpoint: dict[str, Any] | None = None) -> dict[str, Any]:
    checkpoint_obj = checkpoint or {}
    trace_id = _trace_id_for_projection(task, checkpoint_obj)
    return {
        "id": str(task.get("id", "")).strip(),
        "source": str(task.get("source", "manual")).strip() or "manual",
        "status": derive_status(task),
        "checkpoint_state": str(checkpoint_obj.get("state", "CREATED")).strip().upper() or "CREATED",
        "trace_id": trace_id,
        "updated_at": str(
            task.get("updated_at")
            or checkpoint_obj.get("updated_at")
            or task.get("created_at")
            or ""
        ).strip(),
        "risk_level": str(task.get("risk_level", "low")).strip() or "low",
        "review_requested": bool(task.get("review_requested", True)),
        "workflow_kind": str(task.get("workflow_kind", "")).strip(),
        "task_kind": str(task.get("task_kind", "")).strip(),
        "research_phase": str(checkpoint_obj.get("research_phase", "")).strip(),
        "round": int(checkpoint_obj.get("round", task.get("round", 0)) or 0),
        "proposal_version": int(checkpoint_obj.get("proposal_version", task.get("proposal_version", 0)) or 0),
        "proposal_state": str(checkpoint_obj.get("proposal_state", "")).strip(),
        "research_status": str(checkpoint_obj.get("research_status", "")).strip(),
        "rejected": bool(checkpoint_obj.get("rejected", False)),
        "entered_third_round": bool(checkpoint_obj.get("entered_third_round", False)),
        "degraded": bool(checkpoint_obj.get("degraded", False)),
    }


def _list_checkpoints() -> list[dict[str, Any]]:
    checkpoints: list[dict[str, Any]] = []
    for path in sorted(es.CHECKPOINT_DIR.glob("*.json")):
        payload = _safe_load_json(path)
        if payload:
            checkpoints.append(payload)
    return checkpoints


def _queue_counts() -> dict[str, int]:
    return {
        "pending_actions": len(list(es.ACTIONS_PENDING_DIR.glob("*.json"))),
        "pending_controls": len(list(es.CONTROL_PENDING_DIR.glob("*.json"))),
        "done_controls": len(list(es.CONTROL_DONE_DIR.glob("*.json"))),
        "failed_controls": len(list(es.CONTROL_FAILED_DIR.glob("*.json"))),
    }


def _watchdog_summary() -> dict[str, Any]:
    timeout_sec = max(30, int(os.getenv("AGN_WATCHDOG_TIMEOUT_SEC", "300") or "300"))
    checkpoints = _list_checkpoints()
    running = [cp for cp in checkpoints if str(cp.get("state", "")).strip().upper() in {"EXEC_RUNNING", "REVIEW_RUNNING"}]
    stale = 0
    now = datetime.now(tz=timezone.utc)
    for cp in running:
        parsed = _parse_iso(str(cp.get("last_event_time", "")).strip())
        if parsed is None:
            continue
        if (now - parsed).total_seconds() >= timeout_sec:
            stale += 1
    return {
        "running_count": len(running),
        "stale_running_count": stale,
        "timeout_sec": timeout_sec,
    }


def _last_tick_utc() -> str:
    latest: datetime | None = None
    for path in sorted(es.EVENTS_DIR.glob("*.jsonl")):
        trace_id = path.stem
        events = es.load_events(trace_id)
        for event in reversed(events):
            if str(event.get("event_type", "")).strip() != "HEARTBEAT_TICK":
                continue
            parsed = _parse_iso(str(event.get("ts", "")).strip())
            if parsed is None:
                break
            if latest is None or parsed > latest:
                latest = parsed
            break
    return latest.isoformat() if latest is not None else ""


def _resolve_object_ref_path(*, ref: str, task_id: str) -> Path:
    parsed = parse_object_ref(ref)
    kind = str(parsed.get("kind", "")).strip()
    trace_id = str(parsed.get("trace_id", "")).strip()
    attempt = max(1, int(parsed.get("attempt", 1) or 1))
    root = _repo_root()

    if kind == "dispatch":
        return (root / "dispatch" / f"{task_id}.json").resolve()
    if kind == "result":
        for suffix in ("json", "md", "txt"):
            candidate = (root / "results" / f"{task_id}.{attempt}.{suffix}").resolve()
            if candidate.exists():
                return candidate
        return (root / "results" / f"{task_id}.{attempt}.json").resolve()
    if kind == "verdict":
        for suffix in ("json", "md", "txt"):
            candidate = (root / "verdicts" / f"{task_id}.{attempt}.{suffix}").resolve()
            if candidate.exists():
                return candidate
        return (root / "verdicts" / f"{task_id}.{attempt}.json").resolve()
    if kind == "snapshot":
        return (es.SNAPSHOT_DIR / f"{trace_id}.snapshot.json").resolve()
    if kind == "events":
        return (es.EVENTS_DIR / f"{trace_id}.jsonl").resolve()
    if kind == "perf":
        return (es.PERF_DIR / f"{trace_id}.perf_summary.json").resolve()
    if kind == "patch":
        return (root / "reports" / f"{task_id}.{attempt}.patch").resolve()
    raise ValueError(f"unsupported_object_kind:{kind}")


def _media_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return "application/json"
    if suffix in {".md", ".txt", ".log", ".jsonl", ".patch", ".diff"}:
        return "text/plain"
    return "application/octet-stream"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_text_excerpt(*, text: str, mode: str, tail_lines: int, max_bytes: int) -> tuple[str, bool]:
    lines = text.splitlines()
    norm_mode = str(mode or "tail").strip().lower()
    if norm_mode == "tail":
        selected = lines[-max(1, int(tail_lines)) :]
    else:
        selected = lines
    rendered = "\n".join(selected)
    encoded = rendered.encode("utf-8")
    if len(encoded) <= max_bytes:
        return rendered, False
    clipped = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return clipped + "\n...<truncated-by-max-bytes>...", True


def _control_payloads_for_status(*, task_id: str, status: str, limit: int) -> list[dict[str, Any]]:
    target = str(status or "pending").strip().lower()
    if target == "pending":
        paths = es.list_pending_control_commands(task_id=task_id)
    elif target == "done":
        paths = sorted(es.CONTROL_DONE_DIR.glob("*.json"))
    elif target == "failed":
        paths = sorted(es.CONTROL_FAILED_DIR.glob("*.json"))
    else:
        raise ValueError("invalid_status")

    items: list[dict[str, Any]] = []
    for path in paths:
        payload = es.load_control_payload(path) if target == "pending" else _safe_load_json(path)
        if not payload:
            continue
        bound_task = str(payload.get("task_id", "")).strip()
        if bound_task and bound_task != task_id:
            continue
        items.append(
            {
                "control_id": str(payload.get("control_id", path.stem)).strip(),
                "type": str(payload.get("control_type", "")).strip().upper(),
                "task_id": bound_task,
                "created_at": str(payload.get("created_at", "")).strip(),
                "status": target,
            }
        )
    return items[-max(1, int(limit)) :]


def _validate_control_input(payload: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    control_type = str(payload.get("control_type", "")).strip().upper()
    if control_type not in _ALLOWED_CONTROL_TYPES:
        raise ValueError("invalid_control_type")

    control_id = str(payload.get("control_id", "")).strip() or f"ctl-{uuid4().hex[:12]}"
    raw_inner = payload.get("payload", {})
    if raw_inner is None:
        raw_inner = {}
    if not isinstance(raw_inner, dict):
        raise ValueError("invalid_payload")

    if control_type == "MODIFY":
        illegal = sorted(set(raw_inner.keys()) - _ALLOWED_MODIFY_FIELDS)
        if illegal:
            raise ValueError(f"unsupported_modify_fields:{','.join(illegal)}")
    if control_type == "FALLBACK_TOPIC" and not str(raw_inner.get("fallback_topic_id", "")).strip():
        raise ValueError("fallback_topic_id_required")
    return control_type, control_id, raw_inner


def _pending_action_projection(task_id: str, trace_id: str) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for path in es.list_pending_actions(task_id=task_id, trace_id=trace_id):
        payload = _safe_load_json(path)
        if not payload:
            continue
        vr = validate_action_payload(payload)
        projected.append(
            {
                "action_id": str(payload.get("action_id", "")).strip(),
                "action_type": str(payload.get("action_type", "")).strip(),
                "state_hint": str(payload.get("state_hint", "")).strip(),
                "created_at": str(payload.get("created_at", "")).strip(),
                "source_role": str(payload.get("source_role", "")).strip(),
                "trace_id": str(payload.get("trace_id", "")).strip(),
                "task_id": str(payload.get("task_id", "")).strip(),
                "refs": payload.get("refs", {}),
                "schema_valid": bool(vr.valid),
            }
        )
    return projected


def _timeline_projection(*, task_id: str, trace_id: str, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in es.load_events(trace_id):
        if str(event.get("task_id", "")).strip() != task_id:
            continue
        event_type = str(event.get("event_type", "")).strip()
        if event_type not in _TIMELINE_EVENT_TYPES:
            continue
        rows.append(
            {
                "event_id": str(event.get("event_id", "")).strip(),
                "event_type": event_type,
                "action_id": str(event.get("action_id", "")).strip(),
                "ts": str(event.get("ts", "")).strip(),
                "severity": str(event.get("severity", "info")).strip() or "info",
                "payload": event.get("payload", {}),
            }
        )
    return rows[-max(1, int(limit)) :]


def _trace_events_projection(*, trace_id: str, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    digests = es.recent_event_digests(trace_id=trace_id, limit=max(1, int(limit)))
    for digest in digests:
        rows.append(
            {
                "event_id": str(digest.get("event_id", "")).strip(),
                "event_type": str(digest.get("event_type", "")).strip(),
                "action_id": str(digest.get("action_id", "")).strip(),
                "ts": str(digest.get("ts", "")).strip(),
                "severity": str(digest.get("severity", "info")).strip() or "info",
                "refs": digest.get("refs", []),
                "payload_preview": str(digest.get("payload_preview", "")),
            }
        )
    return rows


def _message_projection(*, task_id: str, trace_id: str, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in es.load_events(trace_id):
        if str(event.get("task_id", "")).strip() != task_id:
            continue
        if str(event.get("event_type", "")).strip() != "RESEARCH_MESSAGE":
            continue
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        rows.append(
            {
                "task_id": task_id,
                "correlation_id": trace_id,
                "event_id": str(event.get("event_id", "")).strip(),
                "ts": str(event.get("ts", "")).strip(),
                "actor": str(payload.get("actor", "")).strip(),
                "role": str(payload.get("role", payload.get("actor", ""))).strip(),
                "surface": str(payload.get("surface", "")).strip(),
                "kind": str(payload.get("kind", "")).strip(),
                "attempt": int(payload.get("attempt", 0) or 0),
                "round": int(payload.get("round", 0) or 0),
                "message_ref": str(payload.get("message_ref", "")).strip(),
                "packet_chars": int(payload.get("packet_chars", 0) or 0),
                "preview": str(payload.get("preview", "")).strip(),
                "sha256": str(payload.get("sha256", "")).strip(),
                "in_reply_to": str(payload.get("in_reply_to", "")).strip(),
            }
        )
    return rows[-max(1, int(limit)) :]


def create_app(config: AppConfig | None = None) -> FastAPI:
    service = PhaseAService(config or load_config())
    io_threads = 32

    @asynccontextmanager
    async def app_lifespan(_app: FastAPI) -> Any:
        # P3-24: 32 threads is sufficient for filesystem I/O on a single machine.
        service.executor = ThreadPoolExecutor(max_workers=io_threads, thread_name_prefix="phasea-io")
        loop = asyncio.get_running_loop()
        loop.set_default_executor(service.executor)
        limiter = anyio.to_thread.current_default_thread_limiter()
        if limiter.total_tokens < io_threads:
            limiter.total_tokens = io_threads
        try:
            yield
        finally:
            if service.executor is not None:
                service.executor.shutdown(wait=False, cancel_futures=True)
                service.executor = None

    app = FastAPI(title="Phase A SSOT API", version="0.3.0", lifespan=app_lifespan)

    @app.middleware("http")
    async def local_only_guard(request: Request, call_next: Any) -> Any:
        if service.config.local_only_mode:
            host = _request_client_host(request)
            if not _is_local_client_host(host):
                await _audit_log_async(
                    service,
                    route=str(request.url.path),
                    status=403,
                    task_id=None,
                    action="local_only_block",
                    client_host=host,
                )
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "Local-only mode: remote access is disabled"},
                )
        return await call_next(request)

    app.mount("/static", StaticFiles(directory=_static_dir()), name="static")

    @app.get("/dashboard", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(_static_dir() / "agn_console.html")

    @app.get("/api/agn/v1/overview")
    async def agn_overview() -> dict[str, Any]:
        tasks_raw = await anyio.to_thread.run_sync(service.store.list_tasks)
        task_counts: dict[str, int] = {}
        for task in tasks_raw:
            task_id = str(task.get("id", "")).strip()
            checkpoint = es.load_checkpoint(task_id) if task_id else {}
            state = str((checkpoint or {}).get("state", "CREATED")).strip().upper() or "CREATED"
            task_counts[state] = int(task_counts.get(state, 0)) + 1
        payload = {
            "task_counts_by_state": task_counts,
            "queue_counts": _queue_counts(),
            "watchdog_summary": _watchdog_summary(),
            "last_tick_utc": _last_tick_utc(),
        }
        await _audit_log_async(service, route="/api/agn/v1/overview", status=200, task_id=None, action="read_api")
        return payload

    @app.get("/api/agn/v1/tasks")
    async def agn_list_tasks(
        state: str = "",
        search: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        tasks_raw = await anyio.to_thread.run_sync(service.store.list_tasks)
        rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for task in tasks_raw:
            task_id = str(task.get("id", "")).strip()
            checkpoint = es.load_checkpoint(task_id) if task_id else {}
            rows.append((_task_projection(task, checkpoint), task))
        rows.sort(
            key=lambda pair: (
                (_parse_iso(str(pair[0].get("updated_at", "")).strip()) or datetime.fromtimestamp(0, tz=timezone.utc)),
                str(pair[0].get("id", "")),
            ),
            reverse=True,
        )

        state_filter = str(state or "").strip().upper()
        if state_filter:
            rows = [pair for pair in rows if str(pair[0].get("checkpoint_state", "")).upper() == state_filter]

        search_filter = str(search or "").strip().lower()
        if search_filter:
            filtered: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for item, original in rows:
                hay = " ".join(
                    [
                        str(item.get("id", "")),
                        str(item.get("source", "")),
                        str(original.get("request_summary", "")),
                        str(original.get("request_text", ""))[:512],
                    ]
                ).lower()
                if search_filter in hay:
                    filtered.append((item, original))
            rows = filtered

        safe_offset = max(0, int(offset or 0))
        safe_limit = max(1, min(500, int(limit or 100)))
        sliced_rows = rows[safe_offset : safe_offset + safe_limit]
        sliced = [item for item, _ in sliced_rows]
        await _audit_log_async(
            service,
            route="/api/agn/v1/tasks",
            status=200,
            task_id=None,
            action="read_api",
            count=len(sliced),
        )
        return {"tasks": sliced, "total": len(rows), "limit": safe_limit, "offset": safe_offset}

    @app.get("/api/agn/v1/tasks/{task_id}")
    async def agn_get_task(task_id: str) -> dict[str, Any]:
        task = await _store_get_task_async(service, task_id)
        if task is None:
            await _audit_log_async(service, route="/api/agn/v1/tasks/{task_id}", status=404, task_id=task_id, action="read_api")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
        checkpoint = es.load_checkpoint(task_id) or {}
        payload = dict(task)
        payload["status"] = derive_status(task)
        payload["trace_id"] = _trace_id_for_projection(task, checkpoint)
        payload["attempt"] = max(1, int(task.get("attempt", 1) or 1))
        payload["checkpoint_state"] = str(checkpoint.get("state", "CREATED")).strip().upper() or "CREATED"
        payload["task_kind"] = str(task.get("task_kind", "")).strip()
        payload["research_phase"] = str(checkpoint.get("research_phase", "")).strip()
        payload["round"] = int(checkpoint.get("round", task.get("round", 0)) or 0)
        payload["proposal_version"] = int(checkpoint.get("proposal_version", task.get("proposal_version", 0)) or 0)
        payload["proposal_state"] = str(checkpoint.get("proposal_state", "")).strip()
        payload["research_status"] = str(checkpoint.get("research_status", "")).strip()
        payload["rejected"] = bool(checkpoint.get("rejected", False))
        payload["entered_third_round"] = bool(checkpoint.get("entered_third_round", False))
        payload["degraded"] = bool(checkpoint.get("degraded", False))
        await _audit_log_async(service, route="/api/agn/v1/tasks/{task_id}", status=200, task_id=task_id, action="read_api")
        return payload

    @app.get("/api/agn/v1/tasks/{task_id}/checkpoint")
    async def agn_get_checkpoint(task_id: str) -> dict[str, Any]:
        task = await _store_get_task_async(service, task_id)
        if task is None:
            await _audit_log_async(
                service,
                route="/api/agn/v1/tasks/{task_id}/checkpoint",
                status=404,
                task_id=task_id,
                action="read_api",
            )
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
        checkpoint = es.load_checkpoint(task_id) or {}
        if not checkpoint:
            checkpoint = {
                "task_id": task_id,
                "trace_id": _trace_id_for_projection(task, {}),
                "state": "CREATED",
                "paused": False,
                "spec_revision": 0,
                "last_event_time": "",
            }
        await _audit_log_async(
            service,
            route="/api/agn/v1/tasks/{task_id}/checkpoint",
            status=200,
            task_id=task_id,
            action="read_api",
        )
        return checkpoint

    @app.get("/api/agn/v1/tasks/{task_id}/timeline")
    async def agn_task_timeline(task_id: str, limit: int = 200) -> dict[str, Any]:
        task = await _store_get_task_async(service, task_id)
        if task is None:
            await _audit_log_async(
                service,
                route="/api/agn/v1/tasks/{task_id}/timeline",
                status=404,
                task_id=task_id,
                action="read_api",
            )
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
        trace_id = _trace_id_for_projection(task, es.load_checkpoint(task_id) or {})
        rows = _timeline_projection(task_id=task_id, trace_id=trace_id, limit=max(1, min(500, int(limit or 200))))
        await _audit_log_async(
            service,
            route="/api/agn/v1/tasks/{task_id}/timeline",
            status=200,
            task_id=task_id,
            action="read_api",
            count=len(rows),
        )
        return {"task_id": task_id, "trace_id": trace_id, "events": rows}

    @app.get("/api/agn/v1/tasks/{task_id}/pending-actions")
    async def agn_pending_actions(task_id: str) -> dict[str, Any]:
        task = await _store_get_task_async(service, task_id)
        if task is None:
            await _audit_log_async(
                service,
                route="/api/agn/v1/tasks/{task_id}/pending-actions",
                status=404,
                task_id=task_id,
                action="read_api",
            )
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
        trace_id = _trace_id_for_projection(task, es.load_checkpoint(task_id) or {})
        actions = _pending_action_projection(task_id, trace_id)
        await _audit_log_async(
            service,
            route="/api/agn/v1/tasks/{task_id}/pending-actions",
            status=200,
            task_id=task_id,
            action="read_api",
            count=len(actions),
        )
        return {"task_id": task_id, "trace_id": trace_id, "actions": actions}

    @app.get("/api/agn/v1/tasks/{task_id}/messages")
    async def agn_task_messages(task_id: str, limit: int = 200) -> dict[str, Any]:
        task = await _store_get_task_async(service, task_id)
        if task is None:
            await _audit_log_async(
                service,
                route="/api/agn/v1/tasks/{task_id}/messages",
                status=404,
                task_id=task_id,
                action="read_api",
            )
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
        trace_id = _trace_id_for_projection(task, es.load_checkpoint(task_id) or {})
        rows = _message_projection(task_id=task_id, trace_id=trace_id, limit=max(1, min(500, int(limit or 200))))
        await _audit_log_async(
            service,
            route="/api/agn/v1/tasks/{task_id}/messages",
            status=200,
            task_id=task_id,
            action="read_api",
            count=len(rows),
        )
        return {"task_id": task_id, "trace_id": trace_id, "messages": rows}

    @app.get("/api/agn/v1/tasks/{task_id}/controls")
    async def agn_task_controls(
        task_id: str,
        status_value: str = Query(default="pending", alias="status"),
        limit: int = 100,
    ) -> dict[str, Any]:
        task = await _store_get_task_async(service, task_id)
        if task is None:
            await _audit_log_async(
                service,
                route="/api/agn/v1/tasks/{task_id}/controls",
                status=404,
                task_id=task_id,
                action="read_api",
            )
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
        try:
            rows = _control_payloads_for_status(task_id=task_id, status=status_value, limit=max(1, min(500, int(limit or 100))))
        except ValueError:
            await _audit_log_async(
                service,
                route="/api/agn/v1/tasks/{task_id}/controls",
                status=400,
                task_id=task_id,
                action="read_api",
            )
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid status filter")
        await _audit_log_async(
            service,
            route="/api/agn/v1/tasks/{task_id}/controls",
            status=200,
            task_id=task_id,
            action="read_api",
            count=len(rows),
        )
        return {"task_id": task_id, "controls": rows}

    @app.post("/api/agn/v1/tasks/{task_id}/controls")
    async def agn_enqueue_control(
        task_id: str,
        body: dict[str, Any],
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        try:
            auth = decode_bearer_token(
                authorization,
                secret=service.config.jwt_secret,
                algorithm=service.config.jwt_algorithm,
            )
        except AuthError as exc:
            await _audit_log_async(
                service,
                route="/api/agn/v1/tasks/{task_id}/controls",
                status=401,
                task_id=task_id,
                action="control_enqueue",
            )
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

        task = await _store_get_task_async(service, task_id)
        if task is None:
            await _audit_log_async(
                service,
                route="/api/agn/v1/tasks/{task_id}/controls",
                status=404,
                task_id=task_id,
                action="control_enqueue",
            )
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

        payload = dict(body)
        try:
            control_type, control_id, inner_payload = _validate_control_input(payload)
        except ValueError as exc:
            await _audit_log_async(
                service,
                route="/api/agn/v1/tasks/{task_id}/controls",
                status=400,
                task_id=task_id,
                action="control_enqueue",
                reason=str(exc),
            )
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        if inner_payload.get("request_text_ref") and not str(inner_payload.get("request_text_ref", "")).strip().startswith("agn://"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="request_text_ref must be agn:// ref")

        queue_payload = {
            "control_type": control_type,
            "control_id": control_id,
            "task_id": task_id,
            "payload": inner_payload,
        }
        queue_file = await anyio.to_thread.run_sync(lambda: es.enqueue_control_command(queue_payload))
        checkpoint = es.load_checkpoint(task_id) or {}
        trace_id = _trace_id_for_projection(task, checkpoint)
        es.append_event(
            trace_id=trace_id,
            task_id=task_id,
            event_type="CONTROL_ENQUEUED",
            payload={
                "control_type": control_type,
                "control_id": control_id,
                "queued_by": auth.subject,
            },
        )
        queue_ref = f"agn://object/control/{task_id}/{control_id}"
        await _audit_log_async(
            service,
            route="/api/agn/v1/tasks/{task_id}/controls",
            status=200,
            task_id=task_id,
            action="control_enqueue",
            control_type=control_type,
            control_id=control_id,
            queue_ref=queue_ref,
        )
        return {
            "ok": True,
            "task_id": task_id,
            "control_id": control_id,
            "control_type": control_type,
            "queue_ref": queue_ref,
            "queue_file": str(queue_file.name),
        }

    @app.get("/api/agn/v1/traces/{trace_id}/events")
    async def agn_trace_events(trace_id: str, limit: int = 200) -> dict[str, Any]:
        rows = _trace_events_projection(trace_id=trace_id, limit=max(1, min(500, int(limit or 200))))
        await _audit_log_async(
            service,
            route="/api/agn/v1/traces/{trace_id}/events",
            status=200,
            task_id=None,
            action="read_api",
            trace_id=trace_id,
            count=len(rows),
        )
        return {"trace_id": trace_id, "events": rows}

    @app.get("/api/agn/v1/refs/read")
    async def agn_read_ref(
        ref: str,
        mode: str = "tail",
        tail_lines: int = 120,
        max_bytes: int = _DEFAULT_REF_READ_MAX_BYTES,
        task_id: str = "",
    ) -> dict[str, Any]:
        clean_ref = str(ref or "").strip()
        if not clean_ref.startswith("agn://artifact/") and not clean_ref.startswith("agn://object/"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only agn://artifact or agn://object refs are supported")

        bounded_max = max(256, min(_MAX_REF_READ_BYTES, int(max_bytes or _DEFAULT_REF_READ_MAX_BYTES)))
        bounded_tail = max(10, min(2000, int(tail_lines or 120)))

        try:
            if clean_ref.startswith("agn://artifact/"):
                resolved = resolve_ref_path(clean_ref)
                raw = Path(resolved).read_bytes()
                excerpt, truncated = _read_text_excerpt(
                    text=raw.decode("utf-8", errors="replace"),
                    mode=mode,
                    tail_lines=bounded_tail,
                    max_bytes=bounded_max,
                )
                media_type = _media_type_for(Path(resolved))
            else:
                parsed = parse_object_ref(clean_ref)
                object_kind = str(parsed.get("kind", "")).strip()
                trace_id = str(parsed.get("trace_id", "")).strip()
                task_id_hint = str(task_id or "").strip()
                if not task_id_hint and trace_id and object_kind in {"dispatch", "result", "verdict", "patch"}:
                    for checkpoint in _list_checkpoints():
                        if str(checkpoint.get("trace_id", "")).strip() == trace_id:
                            task_id_hint = str(checkpoint.get("task_id", "")).strip()
                            if task_id_hint:
                                break
                if not task_id_hint and object_kind in {"dispatch", "result", "verdict", "patch"}:
                    raise ValueError("task_id_required_for_object_ref")
                resolved = _resolve_object_ref_path(ref=clean_ref, task_id=task_id_hint)
                raw = resolved.read_bytes()
                excerpt, truncated = _read_text_excerpt(
                    text=raw.decode("utf-8", errors="replace"),
                    mode=mode,
                    tail_lines=bounded_tail,
                    max_bytes=bounded_max,
                )
                media_type = _media_type_for(resolved)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"ref_not_found:{type(exc).__name__}") from exc

        await _audit_log_async(
            service,
            route="/api/agn/v1/refs/read",
            status=200,
            task_id=task_id or None,
            action="read_api",
            ref=clean_ref,
        )
        return {
            "ref": clean_ref,
            "content_excerpt": excerpt,
            "truncated": bool(truncated),
            "media_type": media_type,
            "bytes": len(raw),
            "sha256": _sha256_bytes(raw),
        }

    @app.get("/api/tasks")
    async def list_tasks() -> dict[str, list[dict[str, Any]]]:
        tasks_raw = await anyio.to_thread.run_sync(service.store.list_tasks)
        tasks = [_task_to_response(task) for task in tasks_raw]
        await _audit_log_async(service, route="/api/tasks", status=200, task_id=None)
        return {"tasks": tasks}

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, Any]:
        task = await _store_get_task_async(service, task_id)
        if task is None:
            await _audit_log_async(service, route="/api/tasks/{task_id}", status=404, task_id=task_id)
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

        response = _task_to_response(task)
        await _audit_log_async(service, route="/api/tasks/{task_id}", status=200, task_id=task_id)
        return response

    @app.post("/api/tasks/{task_id}/approve")
    async def approve_task(
        task_id: str,
        authorization: str | None = Header(default=None),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-ID"),
    ) -> dict[str, Any]:
        return await _apply_decision(
            service=service,
            task_id=task_id,
            decision="approved",
            route="/api/tasks/{task_id}/approve",
            authorization=authorization,
            correlation_id=x_correlation_id,
        )

    @app.post("/api/tasks/{task_id}/reject")
    async def reject_task(
        task_id: str,
        authorization: str | None = Header(default=None),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-ID"),
    ) -> dict[str, Any]:
        return await _apply_decision(
            service=service,
            task_id=task_id,
            decision="rejected",
            route="/api/tasks/{task_id}/reject",
            authorization=authorization,
            correlation_id=x_correlation_id,
        )

    @app.post("/api/tasks/{task_id}/unlock")
    async def unlock_task(
        task_id: str,
        authorization: str | None = Header(default=None),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-ID"),
    ) -> dict[str, Any]:
        return await _unlock_task(
            service=service,
            task_id=task_id,
            route="/api/tasks/{task_id}/unlock",
            authorization=authorization,
            correlation_id=x_correlation_id,
        )

    @app.post("/api/tasks/{task_id}/approve-external-publish")
    async def approve_external_publish(
        task_id: str,
        authorization: str | None = Header(default=None),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-ID"),
    ) -> dict[str, Any]:
        return await _approve_external_publish(
            service=service,
            task_id=task_id,
            route="/api/tasks/{task_id}/approve-external-publish",
            authorization=authorization,
            correlation_id=x_correlation_id,
        )

    # ── Command Request endpoints (Role Guard P1-1) ──

    @app.get("/api/command-requests")
    async def list_command_requests() -> dict[str, Any]:
        from scripts.command_request import list_pending_requests
        pending = await anyio.to_thread.run_sync(list_pending_requests)
        await _audit_log_async(service, route="/api/command-requests", status=200, task_id=None)
        return {"requests": pending}

    @app.post("/api/command-requests/{request_id}/approve")
    async def approve_command_request(
        request_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        try:
            decode_bearer_token(authorization, secret=service.config.jwt_secret, algorithm=service.config.jwt_algorithm)
        except AuthError as exc:
            await _audit_log_async(service, route="/api/command-requests/approve", status=401, task_id=None)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
        from scripts.command_request import approve_request
        result = await anyio.to_thread.run_sync(lambda: approve_request(request_id))
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Command request not found")
        await _audit_log_async(service, route="/api/command-requests/approve", status=200, task_id=result.get("task_id"), action="command_request_approved", request_id=request_id)
        return {"ok": True, "request": result}

    @app.post("/api/command-requests/{request_id}/reject")
    async def reject_command_request(
        request_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        try:
            decode_bearer_token(authorization, secret=service.config.jwt_secret, algorithm=service.config.jwt_algorithm)
        except AuthError as exc:
            await _audit_log_async(service, route="/api/command-requests/reject", status=401, task_id=None)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
        from scripts.command_request import reject_request
        result = await anyio.to_thread.run_sync(lambda: reject_request(request_id))
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Command request not found")
        await _audit_log_async(service, route="/api/command-requests/reject", status=200, task_id=result.get("task_id"), action="command_request_rejected", request_id=request_id)
        return {"ok": True, "request": result}

    @app.post("/api/command-requests/execute-approved")
    async def execute_approved_command_requests(
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        try:
            decode_bearer_token(authorization, secret=service.config.jwt_secret, algorithm=service.config.jwt_algorithm)
        except AuthError as exc:
            await _audit_log_async(service, route="/api/command-requests/execute", status=401, task_id=None)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
        from scripts.command_request import execute_approved_requests
        executed = await anyio.to_thread.run_sync(execute_approved_requests)
        await _audit_log_async(service, route="/api/command-requests/execute", status=200, task_id=None, action="command_requests_executed", count=len(executed))
        return {"ok": True, "executed": executed}

    @app.get("/api/events")
    async def stream_events(request: Request) -> StreamingResponse:
        client = await service.sse.register()
        await _audit_log_async(
            service,
            route="/api/events",
            status=200,
            task_id=None,
            action="connect",
            client_id=client.client_id,
            clients=await service.sse.client_count(),
        )

        async def event_generator() -> Any:
            try:
                while True:
                    if await request.is_disconnected():
                        break

                    try:
                        event = await asyncio.wait_for(client.queue.get(), timeout=10.0)
                    except TimeoutError:
                        ping = SSEHub.ping_payload()
                        yield SSEHub.encode_event(event_name="ping", data=ping)
                        continue

                    yield SSEHub.encode_event(
                        event_name="task_update",
                        event_id=str(event.get("event_id")),
                        data=event,
                    )
            finally:
                await service.sse.unregister(client.client_id)
                await _audit_log_async(
                    service,
                    route="/api/events",
                    status=200,
                    task_id=None,
                    action="disconnect",
                    client_id=client.client_id,
                    clients=await service.sse.client_count(),
                )

        headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
        return StreamingResponse(event_generator(), media_type="text/event-stream", headers=headers)

    @app.post("/webhooks/github")
    async def github_webhook(
        request: Request,
        x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
        x_github_delivery: str | None = Header(default=None, alias="X-GitHub-Delivery"),
        x_github_event: str | None = Header(default=None, alias="X-GitHub-Event"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-ID"),
    ) -> dict[str, Any]:
        correlation_id = x_correlation_id or str(uuid4())
        raw_body = await request.body()

        valid, reason = _github_signature_valid(service.config.github_webhook_secret, x_hub_signature_256, raw_body)
        if not valid:
            await _audit_log_async(
                service,
                route="/webhooks/github",
                status=401,
                task_id=None,
                action="webhook_rejected",
                source="github",
                reason=reason,
                correlation_id=correlation_id,
            )
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature")

        try:
            payload = _parse_json_bytes(raw_body)
        except ValueError as exc:
            await _audit_log_async(
                service,
                route="/webhooks/github",
                status=400,
                task_id=None,
                action="webhook_rejected",
                source="github",
                reason=str(exc),
                correlation_id=correlation_id,
            )
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload") from exc

        event_id = x_github_delivery or str(payload.get("event_id") or "")
        if not event_id:
            event_id = hashlib.sha256(raw_body).hexdigest()[:24]
        task_id = _webhook_task_id("github", event_id)

        existing = await _store_get_task_async(service, task_id)
        if existing is not None:
            await _audit_log_async(
                service,
                route="/webhooks/github",
                status=200,
                task_id=task_id,
                action="idempotency_hit",
                source="github",
                event_id=event_id,
                correlation_id=correlation_id,
            )
            return {
                "ok": True,
                "dedup": True,
                "task_id": task_id,
                "correlation_id": correlation_id,
            }

        created_at = _utc_now_iso()
        task = {
            "id": task_id,
            "source": "github",
            "event_id": event_id,
            "event_type": x_github_event or payload.get("event_type"),
            "created_at": created_at,
            "correlation_id": correlation_id,
            "status": "pending",
            "agn_managed": True,
            "review_requested": True,
            "decision": None,
            "qa_retry_count": 0,
            "lock_state": "active",
            "lock_reason": "",
            "locked_at": "",
            "task_kind": "protocol",
            "risk_level": "low",
            "side_effect_level": "read_only",
            "allow_external_publish": False,
            "admin_approved": False,
            "request_text": str(payload.get("title") or payload.get("action") or "github webhook event"),
            "acceptance_criteria": [
                {"id": "AC-1", "text": "executor must write ack with exact criteria echo"},
                {"id": "AC-2", "text": "executor must write result with minimum 5 work logs"},
                {"id": "AC-3", "text": "reviewer issues must reference criterion ids with valid evidence index"},
            ],
        }
        task["status"] = derive_status(task)
        await _store_save_task_async(service, task)

        await _audit_log_async(
            service,
            route="/webhooks/github",
            status=200,
            task_id=task_id,
            action="webhook_received",
            source="github",
            event_id=event_id,
            correlation_id=correlation_id,
        )

        await _broadcast_task_update(
            service=service,
            task_id=task_id,
            status_value=task["status"],
            correlation_id=correlation_id,
            source="github",
            server_ts_utc=created_at,
        )

        return {
            "ok": True,
            "dedup": False,
            "task_id": task_id,
            "correlation_id": correlation_id,
        }

    @app.post("/webhooks/xcode")
    async def xcode_webhook(
        request: Request,
        x_event_id: str | None = Header(default=None, alias="X-Event-ID"),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-ID"),
        x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    ) -> dict[str, Any]:
        correlation_id = x_correlation_id or str(uuid4())
        raw_body = await request.body()

        # P3-21: optional HMAC authentication for Xcode webhooks.
        if service.config.xcode_webhook_secret:
            valid, reason = _github_signature_valid(service.config.xcode_webhook_secret, x_hub_signature_256, raw_body)
            if not valid:
                await _audit_log_async(
                    service,
                    route="/webhooks/xcode",
                    status=401,
                    task_id=None,
                    action="webhook_rejected",
                    source="xcode",
                    reason=reason,
                    correlation_id=correlation_id,
                )
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature")

        try:
            payload = _parse_json_bytes(raw_body)
        except ValueError:
            payload = {"raw_body": raw_body.decode("utf-8", errors="replace")}

        event_id = x_event_id or str(payload.get("event_id") or "")
        if not event_id:
            event_id = hashlib.sha256(raw_body).hexdigest()[:24]

        task_id = _webhook_task_id("xcode", event_id)
        existing = await _store_get_task_async(service, task_id)
        if existing is not None:
            await _audit_log_async(
                service,
                route="/webhooks/xcode",
                status=200,
                task_id=task_id,
                action="idempotency_hit",
                source="xcode",
                event_id=event_id,
                correlation_id=correlation_id,
            )
            return {"ok": True, "dedup": True, "task_id": task_id, "correlation_id": correlation_id}

        created_at = _utc_now_iso()
        task = {
            "id": task_id,
            "source": "xcode",
            "event_id": event_id,
            "created_at": created_at,
            "correlation_id": correlation_id,
            "status": "pending",
            "agn_managed": True,
            "review_requested": True,
            "decision": None,
            "qa_retry_count": 0,
            "lock_state": "active",
            "lock_reason": "",
            "locked_at": "",
            "task_kind": "protocol",
            "risk_level": "low",
            "side_effect_level": "read_only",
            "allow_external_publish": False,
            "admin_approved": False,
            "request_text": str(payload.get("action") or payload.get("status") or "xcode build event"),
            "acceptance_criteria": [
                {"id": "AC-1", "text": "executor must write ack with exact criteria echo"},
                {"id": "AC-2", "text": "executor must write result with minimum 5 work logs"},
                {"id": "AC-3", "text": "reviewer issues must reference criterion ids with valid evidence index"},
            ],
        }
        task["status"] = derive_status(task)
        await _store_save_task_async(service, task)

        await _audit_log_async(
            service,
            route="/webhooks/xcode",
            status=200,
            task_id=task_id,
            action="webhook_received",
            source="xcode",
            event_id=event_id,
            correlation_id=correlation_id,
        )

        await _broadcast_task_update(
            service=service,
            task_id=task_id,
            status_value=task["status"],
            correlation_id=correlation_id,
            source="xcode",
            server_ts_utc=created_at,
        )

        return {"ok": True, "dedup": False, "task_id": task_id, "correlation_id": correlation_id}

    @app.post("/webhooks/telegram")
    async def telegram_webhook(
        request: Request,
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-ID"),
    ) -> dict[str, Any]:
        raw_body = await request.body()

        try:
            payload = _parse_json_bytes(raw_body)
        except ValueError as exc:
            await _audit_log_async(
                service,
                route="/webhooks/telegram",
                status=400,
                task_id=None,
                action="webhook_rejected",
                source="telegram",
                reason=str(exc),
                correlation_id=x_correlation_id or str(uuid4()),
            )
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload") from exc

        chat_id = payload.get("chat_id")
        message_id = payload.get("message_id")
        request_text = payload.get("request_text")
        if chat_id is None or message_id is None or request_text is None:
            correlation_id = str(payload.get("correlation_id") or x_correlation_id or uuid4())
            await _audit_log_async(
                service,
                route="/webhooks/telegram",
                status=400,
                task_id=None,
                action="webhook_rejected",
                source="telegram",
                reason="missing_required_fields",
                correlation_id=correlation_id,
            )
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing required telegram fields")

        correlation_id = str(payload.get("correlation_id") or x_correlation_id or uuid4())
        created_at = str(payload.get("created_at") or _utc_now_iso())

        task_id = _webhook_task_id("telegram", f"{chat_id}-{message_id}")
        existing = await _store_get_task_async(service, task_id)
        if existing is not None:
            await _audit_log_async(
                service,
                route="/webhooks/telegram",
                status=200,
                task_id=task_id,
                action="telegram_dedup_hit",
                source="telegram",
                correlation_id=correlation_id,
                chat_id=chat_id,
                message_id=message_id,
            )
            return {"ok": True, "dedup": True, "task_id": task_id, "correlation_id": correlation_id}

        task = {
            "id": task_id,
            "source": "telegram",
            "chat_id": chat_id,
            "message_id": message_id,
            "request_text": request_text,
            "created_at": created_at,
            "correlation_id": correlation_id,
            "status": "pending",
            "agn_managed": True,
            "review_requested": True,
            "decision": None,
            "qa_retry_count": 0,
            "lock_state": "active",
            "lock_reason": "",
            "locked_at": "",
            "task_kind": "protocol",
            "risk_level": "low",
            "side_effect_level": "read_only",
            "allow_external_publish": False,
            "admin_approved": False,
            "acceptance_criteria": [
                {"id": "AC-1", "text": "executor must write ack with exact criteria echo"},
                {"id": "AC-2", "text": "executor must write result with minimum 5 work logs"},
                {"id": "AC-3", "text": "reviewer issues must reference criterion ids with valid evidence index"},
            ],
        }
        task["status"] = derive_status(task)
        await _store_save_task_async(service, task)

        await _audit_log_async(
            service,
            route="/webhooks/telegram",
            status=200,
            task_id=task_id,
            action="telegram_message_received",
            source="telegram",
            correlation_id=correlation_id,
            chat_id=chat_id,
            message_id=message_id,
        )

        await _broadcast_task_update(
            service=service,
            task_id=task_id,
            status_value=task["status"],
            correlation_id=correlation_id,
            source="telegram",
            server_ts_utc=created_at,
        )

        return {"ok": True, "dedup": False, "task_id": task_id, "correlation_id": correlation_id}

    return app



async def _apply_decision(
    *,
    service: PhaseAService,
    task_id: str,
    decision: str,
    route: str,
    authorization: str | None,
    correlation_id: str | None,
) -> dict[str, Any]:
    try:
        auth = decode_bearer_token(
            authorization,
            secret=service.config.jwt_secret,
            algorithm=service.config.jwt_algorithm,
        )
    except AuthError as exc:
        await _audit_log_async(service, route=route, status=401, task_id=task_id)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    task = await _store_get_task_async(service, task_id)
    if task is None:
        await _audit_log_async(service, route=route, status=404, task_id=task_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    decision_ts = _utc_now_iso()
    task["decision"] = decision
    task["reviewed_by"] = auth.subject
    task["reviewed_at"] = decision_ts
    await _store_save_task_async(service, task)

    response = _task_to_response(task)
    await _audit_log_async(service, route=route, status=200, task_id=task_id)

    resolved_correlation_id = correlation_id or str(uuid4())
    await _broadcast_task_update(
        service=service,
        task_id=task_id,
        status_value=response["status"],
        correlation_id=resolved_correlation_id,
        source="api",
        decision=decision,
        server_ts_utc=decision_ts,
    )

    return response


async def _authorize_or_401(
    *,
    service: PhaseAService,
    route: str,
    task_id: str,
    authorization: str | None,
) -> Any:
    try:
        return decode_bearer_token(
            authorization,
            secret=service.config.jwt_secret,
            algorithm=service.config.jwt_algorithm,
        )
    except AuthError as exc:
        await _audit_log_async(service, route=route, status=401, task_id=task_id)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


async def _unlock_task(
    *,
    service: PhaseAService,
    task_id: str,
    route: str,
    authorization: str | None,
    correlation_id: str | None,
) -> dict[str, Any]:
    auth = await _authorize_or_401(service=service, route=route, task_id=task_id, authorization=authorization)
    task = await _store_get_task_async(service, task_id)
    if task is None:
        await _audit_log_async(service, route=route, status=404, task_id=task_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    unlocked_at = _utc_now_iso()
    task["lock_state"] = "active"
    task["lock_reason"] = ""
    task["locked_at"] = ""
    task["qa_retry_count"] = 0
    task["unlocked_by"] = auth.subject
    task["unlocked_at"] = unlocked_at
    task.pop("lock_notified_at", None)
    await _store_save_task_async(service, task)

    await _audit_log_async(
        service,
        route=route,
        status=200,
        task_id=task_id,
        action="task_unlocked",
        unlocked_by=auth.subject,
    )

    response = _task_to_response(task)
    await _broadcast_task_update(
        service=service,
        task_id=task_id,
        status_value=response["status"],
        correlation_id=correlation_id or str(uuid4()),
        source="api",
        decision=None,
        server_ts_utc=unlocked_at,
    )
    return response


async def _approve_external_publish(
    *,
    service: PhaseAService,
    task_id: str,
    route: str,
    authorization: str | None,
    correlation_id: str | None,
) -> dict[str, Any]:
    auth = await _authorize_or_401(service=service, route=route, task_id=task_id, authorization=authorization)
    task = await _store_get_task_async(service, task_id)
    if task is None:
        await _audit_log_async(service, route=route, status=404, task_id=task_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    approved_at = _utc_now_iso()
    task["allow_external_publish"] = True
    task["admin_approved"] = True
    task["admin_approved_by"] = auth.subject
    task["admin_approved_at"] = approved_at
    await _store_save_task_async(service, task)

    await _audit_log_async(
        service,
        route=route,
        status=200,
        task_id=task_id,
        action="external_publish_approved",
        approved_by=auth.subject,
    )

    response = _task_to_response(task)
    await _broadcast_task_update(
        service=service,
        task_id=task_id,
        status_value=response["status"],
        correlation_id=correlation_id or str(uuid4()),
        source="api",
        decision=None,
        server_ts_utc=approved_at,
    )
    return response


app = create_app()
