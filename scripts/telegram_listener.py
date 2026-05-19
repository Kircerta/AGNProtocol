#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from agn_api.ssot_store import SSOTStore
from agent_runner import append_audit, atomic_write_json
from event_sourcing import enqueue_control_command, load_checkpoint, load_events
from network_runtime import (
    BRIEFING_MD_PATH,
    effective_windows,
    load_autonomy_config,
    publish_runtime_surface,
    render_help_text,
    save_autonomy_config,
)
from research_flow import _ensure_task as ensure_research_task
try:
    from research_runtime import resolve_telegram_bot_token
except ImportError:  # pragma: no cover - package import fallback
    from scripts.research_runtime import resolve_telegram_bot_token

RUNTIME_DIR = ROOT / "runtime"
STATE_PATH = RUNTIME_DIR / "telegram_state.json"
CORR_MAP_PATH = RUNTIME_DIR / "telegram_corr_map.json"
AUTONOMY_STATE_PATH = RUNTIME_DIR / "research_autonomy_state.json"

DEFAULT_ACCEPTANCE_CRITERIA: list[dict[str, str]] = [
    {"id": "AC-1", "text": "apply requested fix and keep changes on work branch"},
    {"id": "AC-2", "text": "provide executable verification evidence"},
    {"id": "AC-3", "text": "produce reviewer-traceable verdict"},
]
VALID_TASK_KINDS = {"protocol", "repo"}
EXPLICIT_TASK_HEADERS = (
    "TASK_ID=",
    "CORRELATION_ID=",
    "TASK_KIND=",
    "REPO_PATH=",
    "WORK_BRANCH=",
    "REQUEST_TEXT=",
    "EXECUTOR_PROVIDER=",
    "REVIEWER_PROVIDER=",
    "ACCEPTANCE_CRITERIA=",
    "CRITERIA=",
)


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def load_json_or_default(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(default)
    if isinstance(data, dict):
        return data
    return dict(default)


def _load_listener_state() -> dict[str, Any]:
    state = load_json_or_default(STATE_PATH, {"last_update_id": 0, "research_sessions": {}})
    sessions = state.get("research_sessions")
    if not isinstance(sessions, dict):
        state["research_sessions"] = {}
    return state


def _save_listener_state(state: dict[str, Any]) -> None:
    atomic_write_json(STATE_PATH, state)


def _pending_research_session(chat_id: str) -> dict[str, Any]:
    state = _load_listener_state()
    sessions = state.get("research_sessions", {})
    if not isinstance(sessions, dict):
        return {}
    payload = sessions.get(str(chat_id), {})
    return payload if isinstance(payload, dict) else {}


def _set_pending_research_session(chat_id: str, payload: dict[str, Any]) -> None:
    state = _load_listener_state()
    sessions = state.setdefault("research_sessions", {})
    if not isinstance(sessions, dict):
        sessions = {}
        state["research_sessions"] = sessions
    sessions[str(chat_id)] = dict(payload)
    _save_listener_state(state)


def _clear_pending_research_session(chat_id: str) -> None:
    state = _load_listener_state()
    sessions = state.get("research_sessions", {})
    if isinstance(sessions, dict) and str(chat_id) in sessions:
        sessions.pop(str(chat_id), None)
        _save_listener_state(state)


def _load_autonomy_state() -> dict[str, Any]:
    state = load_json_or_default(AUTONOMY_STATE_PATH, {"windows": {}, "days": {}})
    if not isinstance(state.get("days"), dict):
        state["days"] = {}
    return state


def _save_autonomy_state(state: dict[str, Any]) -> None:
    atomic_write_json(AUTONOMY_STATE_PATH, state)


def _extract_manual_research_fields(text: str) -> dict[str, str]:
    fields = {
        "question": "",
        "hypothesis": "",
        "research_axis": "",
        "baseline": "",
        "single_change": "",
    }
    for line in str(text or "").splitlines():
        raw = str(line).strip()
        lower = raw.lower()
        if lower.startswith("research question:"):
            fields["question"] = raw.split(":", 1)[1].strip()
        elif lower.startswith("hypothesis:"):
            fields["hypothesis"] = raw.split(":", 1)[1].strip()
        elif lower.startswith("research axis:"):
            fields["research_axis"] = raw.split(":", 1)[1].strip()
        elif lower.startswith("baseline:"):
            fields["baseline"] = raw.split(":", 1)[1].strip()
        elif lower.startswith("single change:"):
            fields["single_change"] = raw.split(":", 1)[1].strip()
    return fields


def parse_allowed_chat_ids(raw: str) -> set[str] | None:
    values = [item.strip() for item in raw.split(",") if item.strip()]
    if not values:
        return None
    return set(values)


def normalize_criteria(raw: Any) -> list[dict[str, str]]:
    if raw is None:
        return [dict(item) for item in DEFAULT_ACCEPTANCE_CRITERIA]
    if not isinstance(raw, list) or not raw:
        return [dict(item) for item in DEFAULT_ACCEPTANCE_CRITERIA]

    normalized: list[dict[str, str]] = []
    for idx, item in enumerate(raw, start=1):
        if isinstance(item, dict):
            text = str(item.get("text", "")).strip()
            cid = str(item.get("id", "")).strip() or f"AC-{idx}"
            if text:
                normalized.append({"id": cid, "text": text})
            continue
        if isinstance(item, str):
            text = item.strip()
            if text:
                normalized.append({"id": f"AC-{idx}", "text": text})
    return normalized or [dict(item) for item in DEFAULT_ACCEPTANCE_CRITERIA]


def normalize_task_kind(raw: Any, *, repo_path: str, work_branch: str) -> str:
    kind = str(raw or "").strip().lower()
    if kind in VALID_TASK_KINDS:
        return kind
    if repo_path or work_branch:
        return "repo"
    return "protocol"


def parse_compact_text(text: str) -> dict[str, Any]:
    repo_path = ""
    work_branch = ""
    task_id = ""
    correlation_id = ""
    task_kind = ""
    executor_provider = ""
    reviewer_provider = ""
    explicit_request_text = ""
    request_lines: list[str] = []

    for line in text.splitlines():
        raw = line.strip()
        if not raw:
            request_lines.append("")
            continue
        if raw.startswith("REPO_PATH="):
            repo_path = raw.split("=", 1)[1].strip()
            continue
        if raw.startswith("WORK_BRANCH="):
            work_branch = raw.split("=", 1)[1].strip()
            continue
        if raw.startswith("TASK_ID="):
            task_id = raw.split("=", 1)[1].strip()
            continue
        if raw.startswith("CORRELATION_ID="):
            correlation_id = raw.split("=", 1)[1].strip()
            continue
        if raw.startswith("TASK_KIND="):
            task_kind = raw.split("=", 1)[1].strip()
            continue
        if raw.startswith("EXECUTOR_PROVIDER="):
            executor_provider = raw.split("=", 1)[1].strip()
            continue
        if raw.startswith("REVIEWER_PROVIDER="):
            reviewer_provider = raw.split("=", 1)[1].strip()
            continue
        if raw.startswith("REQUEST_TEXT="):
            explicit_request_text = raw.split("=", 1)[1].strip()
            continue
        request_lines.append(line)

    request_text = explicit_request_text or "\n".join(request_lines).strip()
    return {
        "task_id": task_id,
        "repo_path": repo_path,
        "work_branch": work_branch,
        "request_text": request_text,
        "correlation_id": correlation_id,
        "task_kind": task_kind,
        "executor_provider": executor_provider,
        "reviewer_provider": reviewer_provider,
    }


def parse_message_payload(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        return {}

    try:
        decoded = json.loads(stripped)
        if isinstance(decoded, dict):
            return decoded
    except json.JSONDecodeError:
        pass

    return parse_compact_text(text)


def _looks_like_explicit_task_payload(text: str, payload: dict[str, Any]) -> bool:
    stripped = str(text or "").strip()
    if not stripped:
        return False

    request_text = str(payload.get("request_text") or payload.get("text") or "").strip()
    if not request_text:
        return False

    if stripped.startswith("{"):
        return True

    for line in stripped.splitlines():
        raw = str(line or "").strip()
        if not raw:
            continue
        if any(raw.startswith(header) for header in EXPLICIT_TASK_HEADERS):
            return True
    return False


def call_coordinator(payload: dict[str, Any], timeout_sec: float) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            [sys.executable, "scripts/coordinator_ingest.py", "--from-stdin"],
            cwd=str(ROOT),
            input=json.dumps(payload, ensure_ascii=True),
            text=True,
            capture_output=True,
            timeout=timeout_sec,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"coordinator timed out after {timeout_sec}s"


def telegram_request(token: str, method: str, payload: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    with httpx.Client(timeout=timeout_sec) as client:
        response = client.post(url, json=payload)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or not data.get("ok", False):
        raise RuntimeError("telegram api returned non-ok")
    return data


def telegram_send_message(
    *,
    token: str | None,
    chat_id: str,
    text: str,
    dry_run: bool,
    timeout_sec: float,
) -> None:
    if dry_run or not token:
        print(f"[telegram_listener] would_send chat_id={chat_id}: {text}")
        return
    telegram_request(
        token,
        "sendMessage",
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout_sec,
    )


def save_corr_mapping(correlation_id: str, task_id: str, chat_id: str) -> None:
    data = load_json_or_default(CORR_MAP_PATH, {"map": {}})
    mapping = data.setdefault("map", {})
    if not isinstance(mapping, dict):
        mapping = {}
        data["map"] = mapping
    mapping[correlation_id] = {
        "task_id": task_id,
        "chat_id": chat_id,
        "updated_at": utc_now_iso(),
    }
    atomic_write_json(CORR_MAP_PATH, data)


def should_allow_chat(chat_id: str, allowed: set[str] | None) -> bool:
    if allowed is None:
        # Fail-closed: if no allowlist configured, only allow the hardcoded admin chat_id.
        admin_id = os.getenv("AGN_TELEGRAM_ADMIN_CHAT_ID", "").strip()
        if not admin_id:
            return False
        return chat_id == admin_id
    return chat_id in allowed


def _today_iso() -> str:
    return datetime.now().date().isoformat()


def _split_command_args(tokens: list[str]) -> tuple[list[str], dict[str, str]]:
    positional: list[str] = []
    options: dict[str, str] = {}
    for token in tokens:
        raw = str(token or "").strip()
        if not raw:
            continue
        if "=" in raw:
            key, value = raw.split("=", 1)
            clean_key = key.strip().lower()
            if clean_key:
                options[clean_key] = value.strip()
            continue
        positional.append(raw)
    return positional, options


def _load_research_tasks() -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    ssot_dir = ROOT / "ssot"
    if not ssot_dir.exists():
        return tasks
    for path in sorted(ssot_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("task_kind", "")).strip() != "daily_research":
            continue
        tasks.append(payload)
    return tasks


def _resolve_research_task_id(raw_task_id: str, chat_id: str) -> str:
    clean = str(raw_task_id or "").strip()
    if clean:
        return clean

    chosen_task_id = ""
    chosen_updated_at = ""
    for task in _load_research_tasks():
        task_id = str(task.get("id", "")).strip()
        if not task_id:
            continue
        bound_chat = str(task.get("chat_id", "")).strip()
        if bound_chat and bound_chat != chat_id:
            continue
        checkpoint = load_checkpoint(task_id) or {}
        phase = str(checkpoint.get("research_phase", "")).strip()
        state = str(checkpoint.get("state", "")).strip()
        updated_at = str(task.get("updated_at", task.get("created_at", ""))).strip()
        if phase != "done" and state != "DELIVERED":
            if updated_at >= chosen_updated_at:
                chosen_task_id = task_id
                chosen_updated_at = updated_at
            continue
        if not chosen_task_id and updated_at >= chosen_updated_at:
            chosen_task_id = task_id
            chosen_updated_at = updated_at
    return chosen_task_id


def _research_status_text(task_id: str) -> str:
    store = SSOTStore(ROOT / "ssot")
    task = store.get_task(task_id) or {}
    checkpoint = load_checkpoint(task_id) or {}
    trace_id = str(checkpoint.get("trace_id", task.get("correlation_id", ""))).strip()
    events = load_events(trace_id) if trace_id else []
    latest_event = events[-1] if events else {}
    latest_event_type = str(latest_event.get("event_type", "")).strip() or str(checkpoint.get("recent_event_label", "")).strip() or "n/a"
    return (
        f"[AGN research] status\n"
        f"task_id={task_id}\n"
        f"phase={str(checkpoint.get('research_phase', 'unknown')).strip() or 'unknown'}\n"
        f"state={str(checkpoint.get('state', 'unknown')).strip() or 'unknown'}\n"
        f"round={int(checkpoint.get('round', 0) or 0)}\n"
        f"rejected={bool(checkpoint.get('rejected', False))}\n"
        f"third_round={bool(checkpoint.get('entered_third_round', False))}\n"
        f"degraded={bool(checkpoint.get('degraded', False))}\n"
        f"archived={bool(str(checkpoint.get('archive_ref', '')).strip())}\n"
        f"governance_ready={bool(checkpoint.get('governance_ready', False))}\n"
        f"awaiting_admin_response={bool(checkpoint.get('awaiting_admin_response', False))}\n"
        f"admin_hold_reason={str(checkpoint.get('admin_hold_reason', '')).strip() or 'n/a'}\n"
        f"admin_hold_until={str(checkpoint.get('admin_hold_until', '')).strip() or str(checkpoint.get('daily_brief_deadline', '')).strip() or 'n/a'}\n"
        f"protocol_blocked={bool(checkpoint.get('protocol_blocked', False))}\n"
        f"protocol_block_reason={str(checkpoint.get('protocol_block_reason', '')).strip() or 'n/a'}\n"
        f"empirical_execution={bool(checkpoint.get('empirical_execution', False))}\n"
        f"truthfulness_status={str(checkpoint.get('truthfulness_status', '')).strip() or 'n/a'}\n"
        f"truthfulness_reason={str(checkpoint.get('truthfulness_reason', '')).strip() or 'n/a'}\n"
        f"admin_delivery_status={str(checkpoint.get('admin_delivery_status', '')).strip() or 'n/a'}\n"
        f"executor={str(task.get('executor_provider', '')).strip() or 'n/a'}\n"
        f"reviewer={str(task.get('reviewer_provider', '')).strip() or 'n/a'}\n"
        f"recent_event={latest_event_type}\n"
        f"trace_id={trace_id or 'n/a'}\n"
        f"trace_index_ref={str(checkpoint.get('trace_index_ref', '')).strip() or 'n/a'}\n"
        f"archive_ref={str(checkpoint.get('archive_ref', '')).strip() or 'n/a'}\n"
        f"result_ref={str(checkpoint.get('paper_ref', '')).strip() or str(checkpoint.get('failure_note_ref', '')).strip() or 'n/a'}\n"
        f"final_report_ref={str(checkpoint.get('final_report_ref', '')).strip() or 'n/a'}"
    )


def _launch_research_process(
    *,
    task_id: str,
    unit_date: str,
    scenario: str,
    executor_provider: str,
    reviewer_provider: str,
    chat_id: str,
    research_mode: str = "",
    research_axis: str = "",
    question: str = "",
    hypothesis: str = "",
    baseline: str = "",
    single_change: str = "",
    manual_seed_topic_id: str = "",
    awaiting_admin_until: str = "",
    daily_brief_ref: str = "",
    source: str = "telegram",
) -> None:
    log_dir = ROOT / "reports"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"research_{task_id.replace('/', '_')}.log"
    with log_path.open("a", encoding="utf-8") as handle:
        cmd = [
            sys.executable,
            "scripts/research_flow.py",
            "--task-id",
            task_id,
            "--unit-date",
            unit_date,
            "--scenario",
            scenario,
            "--executor-provider",
            executor_provider,
            "--reviewer-provider",
            reviewer_provider,
            "--chat-id",
            chat_id,
            "--source",
            source,
            "--max-steps",
            "32",
        ]
        for flag, value in [
            ("--research-mode", research_mode),
            ("--research-axis", research_axis),
            ("--question", question),
            ("--hypothesis", hypothesis),
            ("--baseline", baseline),
            ("--single-change", single_change),
            ("--manual-seed-topic-id", manual_seed_topic_id),
            ("--awaiting-admin-until", awaiting_admin_until),
            ("--daily-brief-ref", daily_brief_ref),
        ]:
            if str(value or "").strip():
                cmd.extend([flag, str(value)])
        try:
            subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception as exc:
            print(f"[listener] ERROR: failed to launch research process: {type(exc).__name__}: {exc}", file=sys.stderr)


def _active_brief_task_id(chat_id: str, unit_date: str) -> str:
    state = _load_autonomy_state()
    days = state.get("days", {})
    if not isinstance(days, dict):
        return ""
    day = days.get(str(unit_date), {})
    if not isinstance(day, dict):
        return ""
    bound_chat = str(day.get("chat_id", "")).strip()
    if bound_chat and bound_chat != chat_id:
        return ""
    return str(day.get("task_id", "")).strip()


def _mark_autonomy_manual_override(unit_date: str, task_id: str, chat_id: str) -> None:
    state = _load_autonomy_state()
    days = state.setdefault("days", {})
    if not isinstance(days, dict):
        days = {}
        state["days"] = days
    day = days.setdefault(str(unit_date), {})
    if not isinstance(day, dict):
        day = {}
        days[str(unit_date)] = day
    day["task_id"] = str(task_id).strip()
    day["chat_id"] = str(chat_id).strip()
    day["manual_override"] = True
    day["manual_override_at"] = utc_now_iso()
    _save_autonomy_state(state)


def _generated_research_task_id(unit_date: str) -> str:
    clean_date = str(unit_date or _today_iso()).strip() or _today_iso()
    return f"research-{clean_date}-{uuid4().hex[:8]}"


def _should_reuse_active_brief_task(task_id: str, chat_id: str) -> bool:
    clean_task_id = str(task_id or "").strip()
    if not clean_task_id:
        return False
    store = SSOTStore(ROOT / "ssot")
    task = store.get_task(clean_task_id) or {}
    bound_chat = str(task.get("chat_id", "")).strip()
    if bound_chat and bound_chat != chat_id:
        return False
    checkpoint = load_checkpoint(clean_task_id) or {}
    phase = str(checkpoint.get("research_phase", "")).strip().lower()
    state = str(checkpoint.get("state", "")).strip().upper()
    if phase == "done" or state == "DELIVERED":
        return False
    if bool(checkpoint.get("awaiting_admin_response", False)):
        return True
    if str(checkpoint.get("admin_hold_reason", "")).strip() == "brief_reply_window_open":
        return True
    if str(checkpoint.get("daily_brief_ref", "")).strip():
        return True
    return False


def _resolve_research_start_task_id(*, explicit_task_id: str, chat_id: str, unit_date: str) -> str:
    clean_explicit = str(explicit_task_id or "").strip()
    if clean_explicit:
        return clean_explicit
    active_task_id = _active_brief_task_id(chat_id, unit_date)
    if _should_reuse_active_brief_task(active_task_id, chat_id):
        return active_task_id
    return _generated_research_task_id(unit_date)


def _queue_research_start(
    *,
    chat_id: str,
    unit_date: str,
    scenario: str,
    executor_provider: str,
    reviewer_provider: str,
    research_mode: str,
    question: str,
    hypothesis: str,
    baseline: str,
    single_change: str,
    research_axis: str,
    manual_seed_topic_id: str,
    explicit_task_id: str,
    token: str | None,
    dry_run: bool,
    timeout_sec: float,
    source: str,
) -> str:
    task_id = _resolve_research_start_task_id(
        explicit_task_id=explicit_task_id,
        chat_id=chat_id,
        unit_date=unit_date,
    )
    task = ensure_research_task(
        task_id=task_id,
        unit_date=unit_date,
        scenario=scenario,
        executor_provider=executor_provider,
        reviewer_provider=reviewer_provider,
        chat_id=chat_id,
        source=source,
        research_mode=research_mode,
        research_axis=research_axis,
        question=question,
        hypothesis=hypothesis,
        baseline=baseline,
        single_change=single_change,
        manual_seed_topic_id=manual_seed_topic_id,
    )
    save_corr_mapping(str(task.get("correlation_id", "")).strip(), task_id, chat_id)
    _mark_autonomy_manual_override(unit_date, task_id, chat_id)
    if not dry_run:
        _launch_research_process(
            task_id=task_id,
            unit_date=unit_date,
            scenario=scenario,
            executor_provider=executor_provider,
            reviewer_provider=reviewer_provider,
            chat_id=chat_id,
            research_mode=research_mode,
            research_axis=research_axis,
            question=question,
            hypothesis=hypothesis,
            baseline=baseline,
            single_change=single_change,
            manual_seed_topic_id=manual_seed_topic_id,
            source=source,
        )
    publish_runtime_surface(reason="telegram_research_start")
    telegram_send_message(
        token=token,
        chat_id=chat_id,
        text=(
            f"[AGN research] start queued\n"
            f"task_id={task_id}\n"
            f"scenario={scenario}\n"
            f"research_mode={research_mode}\n"
            f"manual_intake_locked={bool(question.strip() and hypothesis.strip())}\n"
            f"publish_preauthorized=true\n"
            f"workflow_started=true\n"
            f"executor={executor_provider}\n"
            f"reviewer={reviewer_provider}\n"
            f"correlation_id={str(task.get('correlation_id', '')).strip()}"
        ),
        dry_run=dry_run,
        timeout_sec=timeout_sec,
    )
    return task_id


def _system_status_text() -> str:
    """Build a compact system health summary from the control plane read model."""
    try:
        overview_path = ROOT / "runtime" / "admin_control" / "read_models" / "overview.json"
        if overview_path.exists():
            overview = json.loads(overview_path.read_text(encoding="utf-8"))
        else:
            from control_plane_read_model import build_overview_model
            overview = build_overview_model()
    except Exception as exc:
        return f"[AGN status] error loading overview: {type(exc).__name__}"

    counts = overview.get("counts", {})
    mode = overview.get("system_mode", {})
    agn1 = overview.get("agn1_subsystem", {})
    failures = overview.get("recent_failures", [])
    estop = bool(mode.get("emergency_stop_active", False))
    accepting = bool(mode.get("dispatcher_accepts_new_work", True))

    # SSOT task counts
    try:
        ssot_dir = ROOT / "ssot"
        ssot_count = len(list(ssot_dir.glob("*.json"))) if ssot_dir.exists() else 0
        halted = 0
        for p in ssot_dir.glob("*.json"):
            try:
                t = json.loads(p.read_text(encoding="utf-8"))
                if str(t.get("lock_state", "")).strip().lower() == "halted":
                    halted += 1
            except Exception:
                continue
    except Exception:
        ssot_count = 0
        halted = 0

    # Provider usage from ledger
    usage_line = ""
    try:
        ledger_path = ROOT / "reports" / "provider_usage.jsonl"
        if ledger_path.exists():
            lines = ledger_path.read_text(encoding="utf-8").strip().splitlines()
            recent = lines[-50:] if lines else []
            provider_tokens: dict[str, int] = {}
            for line in recent:
                try:
                    entry = json.loads(line)
                    prov = str(entry.get("provider", "unknown"))
                    total = int(entry.get("total_tokens", 0))
                    provider_tokens[prov] = provider_tokens.get(prov, 0) + total
                except Exception:
                    continue
            if provider_tokens:
                parts = [f"{p}={t}" for p, t in sorted(provider_tokens.items())]
                usage_line = f"recent_tokens={', '.join(parts)}\n"
    except Exception:
        pass

    lines = [
        "[AGN system status]",
        f"emergency_stop={'ACTIVE' if estop else 'off'}",
        f"accepting_work={'yes' if accepting else 'NO'}",
        f"active_tasks={counts.get('active_tasks', '?')}",
        f"queued={counts.get('queued_tasks', '?')}",
        f"blocked={counts.get('blocked_tasks', '?')}",
        f"dead_letters={counts.get('dead_letters', '?')}",
        f"policy_gate_pending={counts.get('policy_gate_pending', '?')}",
        f"ssot_tasks={ssot_count}",
        f"halted_tasks={halted}",
    ]
    if usage_line:
        lines.append(usage_line.strip())
    if agn1.get("last_event_ts"):
        lines.append(f"agn1_last_event={agn1['last_event_ts']}")
    if failures:
        lines.append(f"recent_failures={len(failures)}")
        for f in failures[-3:]:
            lines.append(f"  {f.get('event_type', '?')} task={f.get('task_id', '?')}")
    return "\n".join(lines)


def _provider_costs_text() -> str:
    """Summarize provider token usage from the usage ledger."""
    ledger_path = ROOT / "reports" / "provider_usage.jsonl"
    if not ledger_path.exists():
        return "[AGN costs] no usage data yet."
    try:
        lines = ledger_path.read_text(encoding="utf-8").strip().splitlines()
    except Exception:
        return "[AGN costs] error reading usage ledger."
    if not lines:
        return "[AGN costs] no usage data yet."

    provider_stats: dict[str, dict[str, int]] = {}
    for line in lines:
        try:
            entry = json.loads(line)
            prov = str(entry.get("provider", "unknown"))
            if prov not in provider_stats:
                provider_stats[prov] = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            provider_stats[prov]["calls"] += 1
            provider_stats[prov]["input_tokens"] += int(entry.get("input_tokens", 0))
            provider_stats[prov]["output_tokens"] += int(entry.get("output_tokens", 0))
            provider_stats[prov]["total_tokens"] += int(entry.get("total_tokens", 0))
        except Exception:
            continue

    if not provider_stats:
        return "[AGN costs] no parsed usage entries."

    parts = ["[AGN provider costs]"]
    for prov, stats in sorted(provider_stats.items()):
        parts.append(
            f"{prov}: calls={stats['calls']} "
            f"in={stats['input_tokens']} out={stats['output_tokens']} "
            f"total={stats['total_tokens']}"
        )
    parts.append(f"entries={len(lines)}")
    return "\n".join(parts)


def _handle_research_command(
    *,
    chat_id: str,
    text: str,
    token: str | None,
    dry_run: bool,
    timeout_sec: float,
    default_executor_provider: str,
    default_reviewer_provider: str,
) -> bool:
    stripped = str(text or "").strip()
    if stripped.lower() == "/agn help":
        publish_runtime_surface(reason="telegram_help")
        telegram_send_message(
            token=token,
            chat_id=chat_id,
            text=render_help_text(),
            dry_run=dry_run,
            timeout_sec=timeout_sec,
        )
        return True
    if stripped.lower() == "/agn status":
        telegram_send_message(
            token=token,
            chat_id=chat_id,
            text=_system_status_text(),
            dry_run=dry_run,
            timeout_sec=timeout_sec,
        )
        return True
    if stripped.lower() == "/agn costs":
        telegram_send_message(
            token=token,
            chat_id=chat_id,
            text=_provider_costs_text(),
            dry_run=dry_run,
            timeout_sec=timeout_sec,
        )
        return True
    if not stripped.lower().startswith("/research"):
        return False

    try:
        tokens = shlex.split(stripped)
    except ValueError:
        tokens = stripped.split()
    if len(tokens) < 2:
        publish_runtime_surface(reason="telegram_help")
        telegram_send_message(
            token=token,
            chat_id=chat_id,
            text=render_help_text(),
            dry_run=dry_run,
            timeout_sec=timeout_sec,
        )
        return True

    action = str(tokens[1]).strip().lower()
    positional, options = _split_command_args(tokens[2:])
    target_task_id = _resolve_research_task_id(
        options.get("task_id") or (positional[0] if action not in {"start", "fallback"} and positional else ""),
        chat_id,
    )
    autonomy_config = load_autonomy_config()

    if action == "start":
        unit_date = str(options.get("date") or _today_iso()).strip() or _today_iso()
        scenario = str(options.get("scenario") or "daily").strip().lower() or "daily"
        if scenario not in {"daily", "validation"}:
            scenario = "daily"
        executor_provider = str(options.get("executor") or default_executor_provider or "codex").strip().lower() or "codex"
        reviewer_provider = str(options.get("reviewer") or default_reviewer_provider or "gemini").strip().lower() or "gemini"
        manual_fields = _extract_manual_research_fields(stripped)
        explicit_task_id = str(options.get("task_id") or "").strip()
        requested_mode = str(options.get("mode") or (positional[0] if positional else "")).strip().lower()
        if requested_mode == "minimal":
            _queue_research_start(
                chat_id=chat_id,
                unit_date=unit_date,
                scenario=scenario,
                executor_provider=executor_provider,
                reviewer_provider=reviewer_provider,
                research_mode="manual",
                question="Run the safest same-day research topic inside the allowed axis set.",
                hypothesis="A fixed-budget safe fallback topic will still produce a valuable mini paper or failure note tonight.",
                baseline="",
                single_change="",
                research_axis="",
                manual_seed_topic_id="local_global_dependency",
                explicit_task_id=explicit_task_id,
                token=token,
                dry_run=dry_run,
                timeout_sec=timeout_sec,
                source="telegram_manual_minimal",
            )
            return True
        if manual_fields["question"] and manual_fields["hypothesis"]:
            _clear_pending_research_session(chat_id)
            _queue_research_start(
                chat_id=chat_id,
                unit_date=unit_date,
                scenario=scenario,
                executor_provider=executor_provider,
                reviewer_provider=reviewer_provider,
                research_mode="manual",
                question=manual_fields["question"],
                hypothesis=manual_fields["hypothesis"],
                baseline=manual_fields["baseline"],
                single_change=manual_fields["single_change"],
                research_axis=manual_fields["research_axis"],
                manual_seed_topic_id="",
                explicit_task_id=explicit_task_id,
                token=token,
                dry_run=dry_run,
                timeout_sec=timeout_sec,
                source="telegram_manual",
            )
            return True
        _set_pending_research_session(
            chat_id,
            {
                "mode": "await_manual_research_input",
                "unit_date": unit_date,
                "scenario": scenario,
                "executor_provider": executor_provider,
                "reviewer_provider": reviewer_provider,
                "task_id": explicit_task_id,
                "requested_at": utc_now_iso(),
            },
        )
        telegram_send_message(
            token=token,
            chat_id=chat_id,
            text=(
                "[AGN research] manual intake opened\n"
                "Reply in the next message with:\n"
                "Research Question: ...\n"
                "Hypothesis: ...\n"
                "Optional:\n"
                "Research Axis: ...\n"
                "Baseline: ...\n"
                "Single Change: ...\n"
                "Or send /research start minimal for the safest same-day topic."
            ),
            dry_run=dry_run,
            timeout_sec=timeout_sec,
        )
        return True

    if action == "status":
        if not target_task_id:
            telegram_send_message(
                token=token,
                chat_id=chat_id,
                text="[AGN research] no research task found.",
                dry_run=dry_run,
                timeout_sec=timeout_sec,
            )
            return True
        telegram_send_message(
            token=token,
            chat_id=chat_id,
            text=_research_status_text(target_task_id),
            dry_run=dry_run,
            timeout_sec=timeout_sec,
        )
        return True

    if action == "windows":
        publish_runtime_surface(reason="telegram_windows_view")
        telegram_send_message(
            token=token,
            chat_id=chat_id,
            text=(
                f"[AGN research] windows\n"
                f"auto_enabled={bool(autonomy_config.get('auto_enabled', False))}\n"
                f"morning={str(autonomy_config.get('morning_window', ''))}\n"
                f"afternoon={str(autonomy_config.get('afternoon_window', ''))}\n"
                f"effective={', '.join(effective_windows(autonomy_config))}\n"
                f"briefing={BRIEFING_MD_PATH}"
            ),
            dry_run=dry_run,
            timeout_sec=timeout_sec,
        )
        return True

    if action == "set-morning":
        value = str(options.get("time") or (positional[0] if positional else "")).strip()
        updated = save_autonomy_config({"morning_window": value})
        publish_runtime_surface(
            reason="telegram_set_morning_window",
            impact_scope=["coordinator", "telegram_management", "autonomy"],
        )
        telegram_send_message(
            token=token,
            chat_id=chat_id,
            text=(
                f"[AGN research] morning window set\n"
                f"morning={str(updated.get('morning_window', ''))}\n"
                f"effective={', '.join(effective_windows(updated))}"
            ),
            dry_run=dry_run,
            timeout_sec=timeout_sec,
        )
        return True

    if action == "set-afternoon":
        value = str(options.get("time") or (positional[0] if positional else "")).strip()
        updated = save_autonomy_config({"afternoon_window": value})
        publish_runtime_surface(
            reason="telegram_set_afternoon_window",
            impact_scope=["coordinator", "telegram_management", "autonomy"],
        )
        telegram_send_message(
            token=token,
            chat_id=chat_id,
            text=(
                f"[AGN research] afternoon window set\n"
                f"afternoon={str(updated.get('afternoon_window', ''))}\n"
                f"effective={', '.join(effective_windows(updated))}"
            ),
            dry_run=dry_run,
            timeout_sec=timeout_sec,
        )
        return True

    if action == "auto":
        desired = str(positional[0] if positional else options.get("state", "")).strip().lower()
        if desired not in {"on", "off"}:
            telegram_send_message(
                token=token,
                chat_id=chat_id,
                text="[AGN research] use /research auto on or /research auto off.",
                dry_run=dry_run,
                timeout_sec=timeout_sec,
            )
            return True
        updated = save_autonomy_config({"auto_enabled": desired == "on"})
        publish_runtime_surface(
            reason="telegram_auto_toggle",
            impact_scope=["coordinator", "telegram_management", "autonomy"],
        )
        telegram_send_message(
            token=token,
            chat_id=chat_id,
            text=(
                f"[AGN research] auto updated\n"
                f"auto_enabled={bool(updated.get('auto_enabled', False))}\n"
                f"effective={', '.join(effective_windows(updated))}"
            ),
            dry_run=dry_run,
            timeout_sec=timeout_sec,
        )
        return True

    if not target_task_id:
        telegram_send_message(
            token=token,
            chat_id=chat_id,
            text="[AGN research] no target task found.",
            dry_run=dry_run,
            timeout_sec=timeout_sec,
        )
        return True

    control_type_map = {
        "pause": "PAUSE",
        "mark-exception": "MARK_ANOMALY",
    }
    if action in control_type_map:
        control_type = control_type_map[action]
        enqueue_control_command(
            {
                "control_type": control_type,
                "task_id": target_task_id,
                "payload": {},
            }
        )
        publish_runtime_surface(
            reason=f"telegram_control_{control_type.lower()}",
            impact_scope=["coordinator", "telegram_management"],
            affects_worker_init=False,
        )
        telegram_send_message(
            token=token,
            chat_id=chat_id,
            text=f"[AGN research] control queued\ncontrol={control_type}\ntask_id={target_task_id}",
            dry_run=dry_run,
            timeout_sec=timeout_sec,
        )
        return True

    if action == "fallback":
        fallback_topic_id = str(options.get("topic") or (positional[0] if positional else "")).strip()
        enqueue_control_command(
            {
                "control_type": "FALLBACK_TOPIC",
                "task_id": target_task_id,
                "payload": {"fallback_topic_id": fallback_topic_id},
            }
        )
        publish_runtime_surface(
            reason="telegram_fallback_topic",
            impact_scope=["coordinator", "telegram_management"],
            affects_worker_init=False,
        )
        telegram_send_message(
            token=token,
            chat_id=chat_id,
            text=(
                f"[AGN research] control queued\n"
                "control=FALLBACK_TOPIC\n"
                f"task_id={target_task_id}\n"
                f"topic={fallback_topic_id or 'auto-safe-fallback'}"
            ),
            dry_run=dry_run,
            timeout_sec=timeout_sec,
        )
        return True

    telegram_send_message(
        token=token,
        chat_id=chat_id,
        text=f"[AGN research] unknown command: {action}\nSee /agn help",
        dry_run=dry_run,
        timeout_sec=timeout_sec,
    )
    return True


def _handle_pending_research_submission(
    *,
    chat_id: str,
    text: str,
    token: str | None,
    dry_run: bool,
    timeout_sec: float,
    default_executor_provider: str,
    default_reviewer_provider: str,
) -> bool:
    fields = _extract_manual_research_fields(text)
    if not fields["question"] or not fields["hypothesis"]:
        return False

    session = _pending_research_session(chat_id)
    active_task_id = _active_brief_task_id(chat_id, _today_iso())
    if not session and not active_task_id:
        return False

    unit_date = str(session.get("unit_date", "")).strip() or _today_iso()
    scenario = str(session.get("scenario", "daily")).strip().lower() or "daily"
    executor_provider = str(session.get("executor_provider", "") or default_executor_provider or "codex").strip().lower() or "codex"
    reviewer_provider = str(session.get("reviewer_provider", "") or default_reviewer_provider or "gemini").strip().lower() or "gemini"
    explicit_task_id = str(session.get("task_id", "")).strip() or active_task_id
    _clear_pending_research_session(chat_id)
    _queue_research_start(
        chat_id=chat_id,
        unit_date=unit_date,
        scenario=scenario,
        executor_provider=executor_provider,
        reviewer_provider=reviewer_provider,
        research_mode="manual",
        question=fields["question"],
        hypothesis=fields["hypothesis"],
        baseline=fields["baseline"],
        single_change=fields["single_change"],
        research_axis=fields["research_axis"],
        manual_seed_topic_id="",
        explicit_task_id=explicit_task_id,
        token=token,
        dry_run=dry_run,
        timeout_sec=timeout_sec,
        source="telegram_manual_reply",
    )
    return True


def process_message(
    *,
    update_id: int,
    chat_id: str,
    message_id: str,
    text: str,
    token: str | None,
    dry_run: bool,
    allowed_chats: set[str] | None,
    timeout_sec: float,
    default_repo_path: str,
    default_work_branch: str,
    default_executor_provider: str,
    default_reviewer_provider: str,
) -> None:
    correlation_id_default = f"tg-{chat_id}-{message_id}"
    if not should_allow_chat(chat_id, allowed_chats):
        append_audit(
            action="telegram_message_rejected",
            task_id=None,
            route="/telegram/listener",
            status=403,
            correlation_id=correlation_id_default,
            chat_id=chat_id,
            message_id=message_id,
            reason="chat_not_allowed",
        )
        telegram_send_message(
            token=token,
            chat_id=chat_id,
            text="[AGN] this chat is not allowed for dispatch.",
            dry_run=dry_run,
            timeout_sec=timeout_sec,
        )
        return

    if _handle_research_command(
        chat_id=chat_id,
        text=text,
        token=token,
        dry_run=dry_run,
        timeout_sec=timeout_sec,
        default_executor_provider=default_executor_provider,
        default_reviewer_provider=default_reviewer_provider,
    ):
        return

    if _handle_pending_research_submission(
        chat_id=chat_id,
        text=text,
        token=token,
        dry_run=dry_run,
        timeout_sec=timeout_sec,
        default_executor_provider=default_executor_provider,
        default_reviewer_provider=default_reviewer_provider,
    ):
        return

    payload = parse_message_payload(text)
    stripped = str(text or "").strip()
    if not _looks_like_explicit_task_payload(text, payload):
        append_audit(
            action="telegram_message_non_task",
            task_id=None,
            route="/telegram/listener",
            status=202,
            correlation_id=correlation_id_default,
            chat_id=chat_id,
            message_id=message_id,
            update_id=update_id,
        )
        if stripped.startswith("/"):
            message = "[AGN] unknown command. Use /agn help for supported task commands."
        else:
            message = (
                "[AGN] plain dialogue was not dispatched.\n"
                "Use /agn help, /research ..., or send an explicit JSON/task envelope for generic AGN tasks."
            )
        telegram_send_message(
            token=token,
            chat_id=chat_id,
            text=message,
            dry_run=dry_run,
            timeout_sec=timeout_sec,
        )
        return

    task_id = str(payload.get("task_id") or f"tg-task-{chat_id}-{message_id}").strip()
    correlation_id = str(payload.get("correlation_id") or correlation_id_default).strip()
    request_text = str(payload.get("request_text") or payload.get("text") or "").strip()
    repo_path = str(payload.get("repo_path") or "").strip()
    work_branch = str(payload.get("work_branch") or payload.get("branch") or "").strip()
    task_kind = normalize_task_kind(payload.get("task_kind"), repo_path=repo_path, work_branch=work_branch)
    if task_kind == "repo":
        repo_path = repo_path or str(default_repo_path or "").strip()
        work_branch = work_branch or str(default_work_branch or "").strip()
    executor_provider = str(payload.get("executor_provider") or default_executor_provider or "codex").strip().lower() or "codex"
    reviewer_provider = str(payload.get("reviewer_provider") or default_reviewer_provider or "gemini").strip().lower() or "gemini"
    acceptance_criteria = normalize_criteria(payload.get("acceptance_criteria") or payload.get("criteria"))

    if not request_text:
        append_audit(
            action="telegram_message_rejected",
            task_id=task_id,
            route="/telegram/listener",
            status=422,
            correlation_id=correlation_id,
            chat_id=chat_id,
            message_id=message_id,
            reason="empty_request_text",
        )
        telegram_send_message(
            token=token,
            chat_id=chat_id,
            text="[AGN] rejected: request_text is empty.",
            dry_run=dry_run,
            timeout_sec=timeout_sec,
        )
        return

    if task_kind == "repo":
        missing_fields: list[str] = []
        if not repo_path:
            missing_fields.append("repo_path")
        if not work_branch:
            missing_fields.append("work_branch")
        if missing_fields:
            append_audit(
                action="telegram_message_rejected",
                task_id=task_id,
                route="/telegram/listener",
                status=422,
                correlation_id=correlation_id,
                chat_id=chat_id,
                message_id=message_id,
                reason="missing_repo_context",
                missing_fields=missing_fields,
            )
            telegram_send_message(
                token=token,
                chat_id=chat_id,
                text=f"[AGN] rejected: task_kind=repo requires {', '.join(missing_fields)}.",
                dry_run=dry_run,
                timeout_sec=timeout_sec,
            )
            return

    dispatch_payload: dict[str, Any] = {
        "task_id": task_id,
        "source": "telegram",
        "correlation_id": correlation_id,
        "task_kind": task_kind,
        "request_text": request_text,
        "repo_path": repo_path,
        "work_branch": work_branch,
        "executor_provider": executor_provider,
        "reviewer_provider": reviewer_provider,
        "acceptance_criteria": acceptance_criteria,
        "chat_id": chat_id,
        "message_id": message_id,
    }

    rc, stdout, stderr = call_coordinator(dispatch_payload, timeout_sec)
    error_message = "dispatch failed. please retry with valid payload."
    response_payload: dict[str, Any] = {}
    if stdout:
        try:
            decoded = json.loads(stdout)
            if isinstance(decoded, dict):
                response_payload = decoded
        except json.JSONDecodeError:
            response_payload = {}

    if rc != 0:
        if response_payload.get("error"):
            error_message = str(response_payload.get("error"))
        append_audit(
            action="telegram_message_failed",
            task_id=task_id,
            route="/telegram/listener",
            status=500,
            correlation_id=correlation_id,
            chat_id=chat_id,
            message_id=message_id,
            update_id=update_id,
        )
        try:
            telegram_send_message(
                token=token,
                chat_id=chat_id,
                text=f"[AGN] {error_message}",
                dry_run=dry_run,
                timeout_sec=timeout_sec,
            )
        except Exception as send_exc:
            print(f"[telegram_listener] failed to send error ack: {type(send_exc).__name__}")
        if stderr:
            print(f"[telegram_listener] coordinator stderr: {stderr}")
        if stdout:
            print(f"[telegram_listener] coordinator stdout: {stdout}")
        return

    attempt = int(response_payload.get("attempt", 0) or 0)
    resolved_task_id = str(response_payload.get("task_id") or task_id).strip() or task_id
    resolved_corr_id = str(response_payload.get("correlation_id") or correlation_id).strip() or correlation_id
    save_corr_mapping(resolved_corr_id, resolved_task_id, chat_id)
    append_audit(
        action="telegram_message_processed",
        task_id=resolved_task_id,
        route="/telegram/listener",
        status=200,
        correlation_id=resolved_corr_id,
        chat_id=chat_id,
        message_id=message_id,
        update_id=update_id,
        attempt=attempt,
    )
    ack_text = (
        f"[AGN] accepted\n"
        f"task_id={resolved_task_id}\n"
        f"attempt={attempt}\n"
        f"correlation_id={resolved_corr_id}"
    )
    try:
        telegram_send_message(
            token=token,
            chat_id=chat_id,
            text=ack_text,
            dry_run=dry_run,
            timeout_sec=timeout_sec,
        )
    except Exception as send_exc:
        print(f"[telegram_listener] failed to send success ack: {type(send_exc).__name__}")


def fetch_updates(token: str, offset: int, timeout_sec: float) -> list[dict[str, Any]]:
    data = telegram_request(
        token,
        "getUpdates",
        {
            "offset": offset,
            "timeout": int(max(1, min(50, timeout_sec))),
            "allowed_updates": ["message"],
        },
        timeout_sec + 5,
    )
    result = data.get("result", [])
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    return []


def run_polling(args: argparse.Namespace) -> int:
    token = resolve_telegram_bot_token()
    if not token:
        print("TELEGRAM_BOT_TOKEN env set: false")
        return 1
    print("TELEGRAM_BOT_TOKEN env set: true")

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    state = _load_listener_state()
    last_update_id = int(state.get("last_update_id", 0) or 0)
    allowed = parse_allowed_chat_ids(os.getenv("ALLOWED_CHAT_IDS", ""))

    while True:
        try:
            updates = fetch_updates(token, last_update_id + 1, args.poll_timeout_seconds)
        except Exception as exc:
            append_audit(
                action="telegram_poll_error",
                task_id=None,
                route="/telegram/listener",
                status=500,
                error=type(exc).__name__,
            )
            if args.once:
                return 1
            time.sleep(max(1.0, args.sleep_seconds))
            continue

        for update in updates:
            update_id = int(update.get("update_id", 0) or 0)
            message = update.get("message")
            if not isinstance(message, dict):
                last_update_id = max(last_update_id, update_id)
                continue
            chat = message.get("chat")
            if not isinstance(chat, dict):
                last_update_id = max(last_update_id, update_id)
                continue
            text = message.get("text")
            if not isinstance(text, str):
                last_update_id = max(last_update_id, update_id)
                continue

            chat_id = str(chat.get("id", "")).strip()
            message_id = str(message.get("message_id", "")).strip()
            if not chat_id or not message_id:
                last_update_id = max(last_update_id, update_id)
                continue

            process_message(
                update_id=update_id,
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                token=token,
                dry_run=False,
                allowed_chats=allowed,
                timeout_sec=args.http_timeout_seconds,
                default_repo_path=str(args.default_repo_path or "").strip(),
                default_work_branch=str(args.default_work_branch or "").strip(),
                default_executor_provider=str(args.default_executor_provider or "codex").strip().lower(),
                default_reviewer_provider=str(args.default_reviewer_provider or "gemini").strip().lower(),
            )
            last_update_id = max(last_update_id, update_id)

        state = _load_listener_state()
        state["last_update_id"] = last_update_id
        _save_listener_state(state)

        if args.once:
            break
        time.sleep(max(0.1, args.sleep_seconds))

    return 0


def run_stdin_once(args: argparse.Namespace) -> int:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    text = sys.stdin.read()
    if not text.strip():
        print("[telegram_listener] stdin payload is empty")
        return 1

    chat_id = str(args.stdin_chat_id)
    message_id = str(args.stdin_message_id or int(time.time()))
    process_message(
        update_id=int(time.time()),
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        token=None,
        dry_run=True,
        allowed_chats=parse_allowed_chat_ids(os.getenv("ALLOWED_CHAT_IDS", "")),
        timeout_sec=args.http_timeout_seconds,
        default_repo_path=str(args.default_repo_path or "").strip(),
        default_work_branch=str(args.default_work_branch or "").strip(),
        default_executor_provider=str(args.default_executor_provider or "codex").strip().lower(),
        default_reviewer_provider=str(args.default_reviewer_provider or "gemini").strip().lower(),
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram listener for AGN dispatch")
    parser.add_argument("--once", action="store_true", help="Run one polling iteration then exit")
    parser.add_argument("--sleep-seconds", type=float, default=1.0)
    parser.add_argument("--poll-timeout-seconds", type=float, default=15.0)
    parser.add_argument("--http-timeout-seconds", type=float, default=20.0)
    parser.add_argument("--stdin", action="store_true", help="Read one telegram message text from stdin")
    parser.add_argument("--stdin-chat-id", default=os.getenv("TELEGRAM_STDIN_CHAT_ID", "local-stdin"))
    parser.add_argument("--stdin-message-id", default="")
    parser.add_argument("--default-repo-path", default=os.getenv("AGN_DEFAULT_REPO_PATH", ""))
    parser.add_argument("--default-work-branch", default=os.getenv("AGN_DEFAULT_WORK_BRANCH", ""))
    parser.add_argument("--default-executor-provider", default=os.getenv("EXECUTOR_PROVIDER", "codex"))
    parser.add_argument("--default-reviewer-provider", default=os.getenv("REVIEWER_PROVIDER", "gemini"))
    args = parser.parse_args()

    append_audit(
        action="telegram_listener_started",
        task_id=None,
        route="/telegram/listener",
        status=200,
        mode="stdin" if args.stdin else "polling",
        once=args.once,
    )

    try:
        if args.stdin:
            rc = run_stdin_once(args)
        else:
            rc = run_polling(args)
    except KeyboardInterrupt:
        rc = 0
    except Exception as exc:
        append_audit(
            action="telegram_listener_crashed",
            task_id=None,
            route="/telegram/listener",
            status=500,
            error=type(exc).__name__,
        )
        print(f"[telegram_listener] fatal: {type(exc).__name__}")
        rc = 1

    append_audit(
        action="telegram_listener_stopped",
        task_id=None,
        route="/telegram/listener",
        status=200 if rc == 0 else 500,
        rc=rc,
    )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
