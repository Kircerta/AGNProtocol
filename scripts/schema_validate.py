#!/usr/bin/env python3
"""Validate AGN protocol JSON files against minimal required-field schemas.

Usage:
    python3 scripts/schema_validate.py --kind dispatch --file dispatch/my-task.json
    python3 scripts/schema_validate.py --kind result   --file results/my-task.1.json
    python3 scripts/schema_validate.py --kind verdict  --file verdicts/my-task.1.json

Exit code: 0 = valid, 1 = invalid (missing fields printed to stderr).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Schema definitions: maps kind -> list of (dotted_path, expected_type | None)
# For arrays-of-objects, we check the first element only.
# ---------------------------------------------------------------------------

_DISPATCH_FIELDS: list[tuple[str, type | None]] = [
    ("task_id", str),
    ("correlation_id", str),
    ("attempt", int),
    ("task_kind", str),
    ("risk_level", str),
    ("side_effect_level", str),
    ("acceptance_criteria", list),
]

_DISPATCH_AC_FIELDS: list[tuple[str, type | None]] = [
    ("id", str),
    ("text", str),
]

_RESULT_FIELDS: list[tuple[str, type | None]] = [
    ("task_id", str),
    ("correlation_id", str),
    ("attempt", int),
    ("result_at", str),
    ("diff_snapshot", str),
    ("work_log", list),
]

_RESULT_WORKLOG_FIELDS: list[tuple[str, type | None]] = [
    ("ts", str),
    ("op", str),
]

_VERDICT_FIELDS: list[tuple[str, type | None]] = [
    ("task_id", str),
    ("correlation_id", str),
    ("attempt", int),
    ("verdict_at", str),
    ("decision", str),
    ("issues", list),
]

_VERDICT_ISSUE_FIELDS: list[tuple[str, type | None]] = [
    ("id", str),
    ("criterion_ref", str),
    ("title", str),
    ("detail", str),
]


# ---------------------------------------------------------------------------
# Validation logic
# ---------------------------------------------------------------------------

def _check_fields(
    data: dict[str, Any],
    fields: list[tuple[str, type | None]],
    prefix: str = "",
) -> list[str]:
    """Return list of human-readable error strings."""
    errors: list[str] = []
    for field_name, expected_type in fields:
        dotted = f"{prefix}{field_name}" if prefix else field_name
        if field_name not in data:
            errors.append(f"missing required field: {dotted}")
        elif expected_type is not None and not isinstance(data[field_name], expected_type):
            actual = type(data[field_name]).__name__
            errors.append(f"wrong type for {dotted}: expected {expected_type.__name__}, got {actual}")
    return errors


def validate_dispatch(data: dict[str, Any]) -> list[str]:
    errors = _check_fields(data, _DISPATCH_FIELDS)
    task_kind = data.get("task_kind")
    if isinstance(task_kind, str) and task_kind not in {"protocol", "repo"}:
        errors.append("task_kind must be 'protocol' or 'repo'")
    risk_level = data.get("risk_level")
    if isinstance(risk_level, str) and risk_level not in {"low", "medium", "high"}:
        errors.append("risk_level must be 'low'|'medium'|'high'")
    side_effect_level = data.get("side_effect_level")
    if isinstance(side_effect_level, str) and side_effect_level not in {"read_only", "local_write", "external_publish"}:
        errors.append("side_effect_level must be 'read_only'|'local_write'|'external_publish'")
    ac = data.get("acceptance_criteria")
    if isinstance(ac, list):
        if len(ac) == 0:
            errors.append("acceptance_criteria must be non-empty")
        for idx, item in enumerate(ac):
            if isinstance(item, dict):
                errors.extend(
                    _check_fields(item, _DISPATCH_AC_FIELDS, prefix=f"acceptance_criteria[{idx}].")
                )
            else:
                errors.append(f"acceptance_criteria[{idx}]: expected object, got {type(item).__name__}")
    return errors


def validate_result(data: dict[str, Any]) -> list[str]:
    errors = _check_fields(data, _RESULT_FIELDS)
    if data.get("decision") is not None:
        if data["decision"] not in ("approve", "reject"):
            errors.append(f"decision must be 'approve' or 'reject', got '{data['decision']}'")
    wl = data.get("work_log")
    if isinstance(wl, list):
        if len(wl) == 0:
            errors.append("work_log must be non-empty")
        for idx, item in enumerate(wl):
            if isinstance(item, dict):
                errors.extend(
                    _check_fields(item, _RESULT_WORKLOG_FIELDS, prefix=f"work_log[{idx}].")
                )
            else:
                errors.append(f"work_log[{idx}]: expected object, got {type(item).__name__}")
    return errors


def validate_verdict(data: dict[str, Any]) -> list[str]:
    errors = _check_fields(data, _VERDICT_FIELDS)
    decision = data.get("decision")
    if isinstance(decision, str) and decision not in ("approve", "reject"):
        errors.append(f"decision must be 'approve' or 'reject', got '{decision}'")
    issues = data.get("issues")
    if isinstance(issues, list):
        for idx, item in enumerate(issues):
            if isinstance(item, dict):
                errors.extend(
                    _check_fields(item, _VERDICT_ISSUE_FIELDS, prefix=f"issues[{idx}].")
                )
                # Check evidence sub-object
                evidence = item.get("evidence")
                if evidence is None:
                    # Backward compatibility for legacy verdict schema.
                    evidence = item.get("evidence_ref")
                if evidence is None:
                    errors.append(f"issues[{idx}].evidence: missing required field")
                elif isinstance(evidence, dict):
                    if "work_log_index" not in evidence:
                        errors.append(f"issues[{idx}].evidence.work_log_index: missing required field")
                    if "work_log_op" not in evidence:
                        errors.append(f"issues[{idx}].evidence.work_log_op: missing required field")
            else:
                errors.append(f"issues[{idx}]: expected object, got {type(item).__name__}")
    return errors


_SSOT_TASK_FIELDS: list[tuple[str, type | None]] = [
    ("id", str),
    ("source", str),
    ("agn_managed", None),
    ("lock_state", str),
]


def validate_ssot_task(data: dict[str, Any]) -> list[str]:
    errors = _check_fields(data, _SSOT_TASK_FIELDS)
    lock_state = data.get("lock_state")
    if isinstance(lock_state, str) and lock_state not in ("active", "halted"):
        errors.append(f"lock_state must be 'active' or 'halted', got '{lock_state}'")
    qa = data.get("qa_retry_count")
    if qa is not None and (not isinstance(qa, int) or qa < 0):
        errors.append(f"qa_retry_count must be a non-negative integer, got {qa!r}")
    status_val = data.get("status")
    if isinstance(status_val, str):
        from agn_api.task_engine import VALID_STATUSES
        if status_val not in VALID_STATUSES:
            errors.append(f"status must be one of {sorted(VALID_STATUSES)}, got '{status_val}'")
    return errors


def validate_role_permissions(data: dict[str, Any]) -> list[str]:
    import re as _re
    errors: list[str] = []
    if "version" not in data:
        errors.append("missing required field: version")
    roles = data.get("roles")
    if not isinstance(roles, dict):
        errors.append("missing or invalid field: roles (expected object)")
        return errors
    for required_role in ("coordinator", "executor", "reviewer", "admin"):
        if required_role not in roles:
            errors.append(f"missing required role: {required_role}")
    for role_name, role_cfg in roles.items():
        if not isinstance(role_cfg, dict):
            errors.append(f"roles.{role_name}: expected object")
            continue
        wd = role_cfg.get("writable_dirs")
        if not isinstance(wd, list):
            errors.append(f"roles.{role_name}.writable_dirs: expected list")
        urc = role_cfg.get("utility_request_commands")
        if not isinstance(urc, list):
            errors.append(f"roles.{role_name}.utility_request_commands: expected list")
        bcp = role_cfg.get("blocked_command_patterns")
        if not isinstance(bcp, list):
            errors.append(f"roles.{role_name}.blocked_command_patterns: expected list")
        else:
            for i, pat in enumerate(bcp):
                if not isinstance(pat, str):
                    errors.append(f"roles.{role_name}.blocked_command_patterns[{i}]: expected string")
                else:
                    try:
                        _re.compile(pat)
                    except _re.error as exc:
                        errors.append(f"roles.{role_name}.blocked_command_patterns[{i}]: invalid regex: {exc}")
        bg = role_cfg.get("blocked_git_subcommands")
        if bg is not None and not isinstance(bg, list):
            errors.append(f"roles.{role_name}.blocked_git_subcommands: expected list")
        elif isinstance(bg, list):
            for i, item in enumerate(bg):
                if not isinstance(item, str) or not item.strip():
                    errors.append(f"roles.{role_name}.blocked_git_subcommands[{i}]: expected non-empty string")
    return errors


def validate_action_protocol(data: dict[str, Any]) -> list[str]:
    from action_protocol import validate_action_payload

    result = validate_action_payload(data)
    return list(result.errors)


VALIDATORS = {
    "dispatch": validate_dispatch,
    "result": validate_result,
    "verdict": validate_verdict,
    "ssot_task": validate_ssot_task,
    "role_permissions": validate_role_permissions,
    "action_protocol": validate_action_protocol,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate AGN protocol JSON files")
    parser.add_argument(
        "--kind",
        required=True,
        choices=sorted(VALIDATORS.keys()),
        help="File kind to validate",
    )
    parser.add_argument(
        "--file",
        required=True,
        help="Path to JSON file",
    )
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"error: file not found: {path}", file=sys.stderr)
        return 1

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"error: invalid JSON: {exc}", file=sys.stderr)
        return 1

    if not isinstance(data, dict):
        print("error: top-level value must be a JSON object", file=sys.stderr)
        return 1

    validator = VALIDATORS[args.kind]
    errors = validator(data)

    if errors:
        print(f"FAIL: {path} ({args.kind})", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(f"OK: {path} ({args.kind})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
