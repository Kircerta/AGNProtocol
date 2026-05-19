#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

try:
    from agn_refs import has_path_semantics, is_agn_ref
except ImportError:  # pragma: no cover - package import fallback
    from scripts.agn_refs import has_path_semantics, is_agn_ref

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "config" / "action_protocol_schema.json"

ACTION_TYPES = {
    "EXECUTE_CMD",
    "WRITE_FILE",
    "READ_REF",
    "READ_REPO_FILE",
    "REQUEST_REVIEW",
    "SUMMARIZE",
    "RETRY",
    "ABORT",
}

DEFAULT_INLINE_LIMIT = max(256, int((__import__("os").environ.get("AGN_ACTION_INLINE_LIMIT", "4096") or "4096")))


@dataclass(frozen=True)
class ActionValidationResult:
    valid: bool
    errors: list[str]


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) or value is None


def _validate_small_inputs(inputs: dict[str, Any], *, inline_limit: int) -> list[str]:
    errors: list[str] = []
    for key, value in inputs.items():
        if not isinstance(key, str) or not key.strip():
            errors.append("inputs keys must be non-empty strings")
            continue
        if isinstance(value, str):
            if len(value) > inline_limit and not key.endswith("_ref"):
                errors.append(f"inputs.{key} too large ({len(value)} chars), use *_ref")
        elif isinstance(value, list):
            if len(value) > 256:
                errors.append(f"inputs.{key} list too large")
            for idx, item in enumerate(value):
                if isinstance(item, str) and len(item) > inline_limit:
                    errors.append(f"inputs.{key}[{idx}] too large ({len(item)} chars), use refs")
                elif not _is_scalar(item):
                    errors.append(f"inputs.{key}[{idx}] must be scalar")
        elif isinstance(value, dict):
            # Nested dicts are allowed only for compact structured args.
            if len(value) > 64:
                errors.append(f"inputs.{key} object too large")
            for sub_key, sub_val in value.items():
                if not isinstance(sub_key, str):
                    errors.append(f"inputs.{key} contains non-string key")
                    continue
                if isinstance(sub_val, str) and len(sub_val) > inline_limit:
                    errors.append(f"inputs.{key}.{sub_key} too large ({len(sub_val)} chars), use refs")
                elif not _is_scalar(sub_val):
                    errors.append(f"inputs.{key}.{sub_key} must be scalar")
        elif not _is_scalar(value):
            errors.append(f"inputs.{key} unsupported type")
    return errors


def _validate_refs(refs: dict[str, Any], *, inline_limit: int) -> list[str]:
    errors: list[str] = []
    for key, value in refs.items():
        if not isinstance(key, str) or not key.strip():
            errors.append("refs keys must be non-empty strings")
            continue
        if isinstance(value, str):
            clean = value.strip()
            if not clean:
                errors.append(f"refs.{key} must be non-empty agn:// ref")
                continue
            if len(clean) > inline_limit * 2:
                errors.append(f"refs.{key} suspiciously large")
            if has_path_semantics(clean):
                errors.append(f"refs.{key} must not contain path semantics")
            if not is_agn_ref(clean):
                errors.append(f"refs.{key} must be agn:// ref")
        elif isinstance(value, list):
            for idx, item in enumerate(value):
                if not isinstance(item, str):
                    errors.append(f"refs.{key}[{idx}] must be string")
                else:
                    clean = item.strip()
                    if not clean:
                        errors.append(f"refs.{key}[{idx}] must be non-empty agn:// ref")
                        continue
                    if len(clean) > inline_limit * 2:
                        errors.append(f"refs.{key}[{idx}] suspiciously large")
                    if has_path_semantics(clean):
                        errors.append(f"refs.{key}[{idx}] must not contain path semantics")
                    if not is_agn_ref(clean):
                        errors.append(f"refs.{key}[{idx}] must be agn:// ref")
        else:
            errors.append(f"refs.{key} must be string or list[string]")
    return errors


def _validate_budget(budget: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = {"max_time_sec", "max_disk_mb", "max_log_kb"}
    missing = sorted(required - set(budget.keys()))
    if missing:
        errors.append(f"budget missing keys: {','.join(missing)}")
    for key in required:
        value = budget.get(key)
        if not isinstance(value, (int, float)) or value <= 0:
            errors.append(f"budget.{key} must be positive number")
    return errors


def validate_action_payload(payload: dict[str, Any], *, inline_limit: int = DEFAULT_INLINE_LIMIT) -> ActionValidationResult:
    errors: list[str] = []
    required = {"trace_id", "task_id", "action_id", "action_type", "inputs", "refs", "budget"}
    allowed = required | {"created_at", "source_role", "state_hint"}

    if not isinstance(payload, dict):
        return ActionValidationResult(False, ["payload must be object"])

    for key in required:
        if key not in payload:
            errors.append(f"missing required field: {key}")
    for key in payload.keys():
        if key not in allowed:
            errors.append(f"unknown field: {key}")

    for key in ("trace_id", "task_id", "action_id"):
        val = payload.get(key)
        if not isinstance(val, str) or not val.strip():
            errors.append(f"{key} must be non-empty string")

    action_type = payload.get("action_type")
    if not isinstance(action_type, str) or action_type not in ACTION_TYPES:
        errors.append(f"action_type must be one of: {','.join(sorted(ACTION_TYPES))}")

    inputs = payload.get("inputs")
    if not isinstance(inputs, dict):
        errors.append("inputs must be object")
    else:
        errors.extend(_validate_small_inputs(inputs, inline_limit=inline_limit))

    refs = payload.get("refs")
    if not isinstance(refs, dict):
        errors.append("refs must be object")
    else:
        errors.extend(_validate_refs(refs, inline_limit=inline_limit))

    budget = payload.get("budget")
    if not isinstance(budget, dict):
        errors.append("budget must be object")
    else:
        errors.extend(_validate_budget(budget))

    created_at = payload.get("created_at")
    if created_at is not None and (not isinstance(created_at, str) or not created_at.strip()):
        errors.append("created_at must be non-empty string when provided")

    return ActionValidationResult(valid=len(errors) == 0, errors=errors)


def build_action(
    *,
    trace_id: str,
    task_id: str,
    action_id: str,
    action_type: str,
    inputs: dict[str, Any],
    refs: dict[str, Any],
    budget: dict[str, Any],
    source_role: str = "coordinator",
    state_hint: str = "",
) -> dict[str, Any]:
    payload = {
        "trace_id": str(trace_id).strip(),
        "task_id": str(task_id).strip(),
        "action_id": str(action_id).strip(),
        "action_type": str(action_type).strip(),
        "inputs": dict(inputs),
        "refs": dict(refs),
        "budget": dict(budget),
        "created_at": utc_now_iso(),
        "source_role": str(source_role).strip() or "coordinator",
    }
    if state_hint:
        payload["state_hint"] = str(state_hint).strip()
    return payload


def load_schema() -> dict[str, Any]:
    if not SCHEMA_PATH.exists():
        return {}
    try:
        loaded = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}
