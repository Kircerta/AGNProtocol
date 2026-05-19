#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

try:
    from action_protocol import build_action
except ImportError:  # pragma: no cover - package import fallback
    from scripts.action_protocol import build_action

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "recovery_policy.json"
DEFAULT_BUDGET = {"max_time_sec": 300, "max_disk_mb": 128, "max_log_kb": 256}

_TERMINAL_STATES = {"DELIVERED", "ABORTED", "NEED_ADMIN"}


@dataclass
class RecoveryDecision:
    actions: list[dict[str, Any]] = field(default_factory=list)
    escalate: bool = False
    escalate_reason: str = ""
    checkpoint_updates: dict[str, Any] = field(default_factory=dict)



def _next_action_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def load_recovery_policy() -> dict[str, Any]:
    if not POLICY_PATH.exists():
        return {
            "retry_limits": {"TIMEOUT_NO_OUTPUT": 2, "TOOL_ERROR": 2, "PROTOCOL_VIOLATION": 1, "GATE_FAIL": 3},
            "degrade_chain": ["summary_only"],
            "backoff": {"base_seconds": 1, "multiplier": 2.0, "max_seconds": 30},
            "escalation": {"max_total_retries": 6, "max_gate_fail_streak": 3, "state": "NEED_ADMIN"},
        }
    try:
        payload = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _latest_trigger(events: list[dict[str, Any]], last_handled_event_id: str) -> tuple[str, dict[str, Any]]:
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        event_id = str(event.get("event_id", "")).strip()
        if event_id and event_id == last_handled_event_id:
            return "", {}
        event_type = str(event.get("event_type", "")).strip()
        if event_type == "TIMEOUT_NO_OUTPUT":
            return "TIMEOUT_NO_OUTPUT", event
        if event_type == "PROTOCOL_VIOLATION":
            return "PROTOCOL_VIOLATION", event
        if event_type == "ACTION_FINISHED":
            payload = event.get("payload", {})
            if isinstance(payload, dict) and int(payload.get("rc", 0) or 0) != 0:
                return "TOOL_ERROR", event
    return "", {}


def _recovery_meta(checkpoint: dict[str, Any]) -> dict[str, Any]:
    raw = checkpoint.get("recovery", {})
    if isinstance(raw, dict):
        counters = raw.get("counters")
        if not isinstance(counters, dict):
            counters = {}
        return {
            "counters": counters,
            "degrade_index": int(raw.get("degrade_index", 0) or 0),
            "total_retries": int(raw.get("total_retries", 0) or 0),
            "last_handled_event_id": str(raw.get("last_handled_event_id", "")).strip(),
        }
    return {"counters": {}, "degrade_index": 0, "total_retries": 0, "last_handled_event_id": ""}


def decide_recovery(
    *,
    trace_id: str,
    task: dict[str, Any],
    checkpoint: dict[str, Any],
    events: list[dict[str, Any]],
    policy: dict[str, Any] | None = None,
) -> RecoveryDecision:
    task_id = str(task.get("id", "")).strip()
    state = str(checkpoint.get("state", "CREATED")).strip().upper() or "CREATED"
    if state in _TERMINAL_STATES:
        return RecoveryDecision()

    active_policy = policy if isinstance(policy, dict) else load_recovery_policy()
    retry_limits = active_policy.get("retry_limits", {}) if isinstance(active_policy.get("retry_limits"), dict) else {}
    degrade_chain = active_policy.get("degrade_chain", []) if isinstance(active_policy.get("degrade_chain"), list) else []
    escalation = active_policy.get("escalation", {}) if isinstance(active_policy.get("escalation"), dict) else {}
    max_total = int(escalation.get("max_total_retries", 6) or 6)
    max_gate_fail = int(escalation.get("max_gate_fail_streak", 3) or 3)

    meta = _recovery_meta(checkpoint)
    updates: dict[str, Any] = {"recovery": dict(meta)}

    gate_fail_streak = int(checkpoint.get("gate_fail_streak", 0) or 0)
    if gate_fail_streak >= max(1, max_gate_fail):
        return RecoveryDecision(
            escalate=True,
            escalate_reason=f"gate_fail_streak_exceeded:{gate_fail_streak}",
            checkpoint_updates=updates,
        )

    trigger_key, trigger_event = _latest_trigger(events, str(meta.get("last_handled_event_id", "")))
    if not trigger_key:
        return RecoveryDecision(checkpoint_updates=updates)

    counters = dict(meta.get("counters", {}))
    seen = int(counters.get(trigger_key, 0) or 0) + 1
    counters[trigger_key] = seen
    total_retries = int(meta.get("total_retries", 0) or 0) + 1
    degrade_index = int(meta.get("degrade_index", 0) or 0)
    last_event_id = str(trigger_event.get("event_id", "")).strip()

    updates["recovery"] = {
        "counters": counters,
        "degrade_index": degrade_index,
        "total_retries": total_retries,
        "last_handled_event_id": last_event_id,
    }

    if total_retries > max(1, max_total):
        return RecoveryDecision(
            escalate=True,
            escalate_reason=f"max_total_retries_exceeded:{total_retries}",
            checkpoint_updates=updates,
        )

    budget = task.get("perf_budget") if isinstance(task.get("perf_budget"), dict) else DEFAULT_BUDGET
    limit = int(retry_limits.get(trigger_key, 0) or 0)
    attempt = max(1, int(task.get("attempt", 1) or 1))

    if seen <= max(0, limit):
        action = build_action(
            trace_id=trace_id,
            task_id=task_id,
            action_id=_next_action_id("retry"),
            action_type="RETRY",
            inputs={
                "reason": trigger_key.lower(),
                "retry_count": seen,
                "last_state": state,
            },
            refs={},
            budget=budget,
            source_role="coordinator",
            state_hint=state,
        )
        return RecoveryDecision(actions=[action], checkpoint_updates=updates)

    if degrade_index < len(degrade_chain):
        strategy = str(degrade_chain[degrade_index]).strip() or "summary_only"
        updates["recovery"]["degrade_index"] = degrade_index + 1
        action = build_action(
            trace_id=trace_id,
            task_id=task_id,
            action_id=_next_action_id("degrade"),
            action_type="SUMMARIZE",
            inputs={
                "attempt": attempt,
                "content": f"apply recovery degrade strategy: {strategy}",
                "strategy": strategy,
                "trigger": trigger_key,
            },
            refs={},
            budget=budget,
            source_role="coordinator",
            state_hint=state,
        )
        return RecoveryDecision(actions=[action], checkpoint_updates=updates)

    return RecoveryDecision(
        escalate=True,
        escalate_reason=f"recovery_exhausted:{trigger_key}",
        checkpoint_updates=updates,
    )
