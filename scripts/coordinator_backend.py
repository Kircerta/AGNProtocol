#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:
    from action_protocol import validate_action_payload
    from agn_refs import find_path_like_values, has_path_semantics, is_agn_ref
    from planner_policy import propose_actions as propose_actions_from_policy
except ImportError:  # pragma: no cover - package import fallback
    from scripts.action_protocol import validate_action_payload
    from scripts.agn_refs import find_path_like_values, has_path_semantics, is_agn_ref
    from scripts.planner_policy import propose_actions as propose_actions_from_policy


@dataclass(frozen=True)
class CoordinatorBackend:
    name: str

    def propose_actions(
        self,
        *,
        snapshot: dict[str, Any],
        recent_event_digests: list[dict[str, Any]],
        control_commands: list[dict[str, Any]],
        ref_index: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        raise NotImplementedError


class BackendProtocolViolation(RuntimeError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors) if errors else "backend_protocol_violation")


def _validate_actions(action_payloads: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for idx, payload in enumerate(action_payloads):
        vr = validate_action_payload(payload)
        if not vr.valid:
            errors.extend([f"actions[{idx}]: {err}" for err in vr.errors])
    return errors


def _validate_remote_inputs(
    *,
    snapshot: dict[str, Any],
    recent_event_digests: list[dict[str, Any]],
    control_commands: list[dict[str, Any]],
    ref_index: list[dict[str, Any]],
) -> list[str]:
    errors: list[str] = []
    task_spec = snapshot.get("task_spec", {})
    if isinstance(task_spec, dict):
        if str(task_spec.get("repo_path", "")).strip():
            errors.append("snapshot.task_spec.repo_path is forbidden in remote backend")
        repo_ref = str(task_spec.get("repo_ref", "")).strip()
        if repo_ref and not is_agn_ref(repo_ref):
            errors.append("snapshot.task_spec.repo_ref must be agn:// ref")

    for idx, item in enumerate(ref_index):
        if not isinstance(item, dict):
            errors.append(f"ref_index[{idx}] must be object")
            continue
        ref = str(item.get("ref", "")).strip()
        if ref and not is_agn_ref(ref):
            errors.append(f"ref_index[{idx}].ref must be agn:// ref")

    for field, value in (
        ("snapshot", snapshot),
        ("recent_event_digests", recent_event_digests),
        ("control_commands", control_commands),
    ):
        hits = find_path_like_values(value, prefix=field)
        if hits:
            errors.extend([f"remote_input_path_semantics:{hit}" for hit in hits[:12]])
    return errors


def _validate_remote_actions(actions: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    errors.extend(_validate_actions(actions))
    forbidden_ref_keys = {"repo_path", "dispatch_path", "result_path", "cwd"}

    for idx, action in enumerate(actions):
        refs = action.get("refs", {})
        if not isinstance(refs, dict):
            errors.append(f"actions[{idx}].refs must be object")
            continue
        for key, value in refs.items():
            if str(key).strip() in forbidden_ref_keys:
                errors.append(f"actions[{idx}].refs.{key} is forbidden")
            if isinstance(value, str):
                if not is_agn_ref(value.strip()):
                    errors.append(f"actions[{idx}].refs.{key} must be agn:// ref")
                elif has_path_semantics(value.strip()):
                    errors.append(f"actions[{idx}].refs.{key} path semantics detected")
            elif isinstance(value, list):
                for j, item in enumerate(value):
                    if not isinstance(item, str) or not is_agn_ref(item.strip()):
                        errors.append(f"actions[{idx}].refs.{key}[{j}] must be agn:// ref")
            else:
                errors.append(f"actions[{idx}].refs.{key} must be string or list[string]")

    return errors


class LocalBackend(CoordinatorBackend):
    def __init__(self) -> None:
        super().__init__(name="local")

    def propose_actions(
        self,
        *,
        snapshot: dict[str, Any],
        recent_event_digests: list[dict[str, Any]],
        control_commands: list[dict[str, Any]],
        ref_index: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        actions = propose_actions_from_policy(
            snapshot=snapshot,
            recent_event_digests=recent_event_digests,
            control_commands=control_commands,
            ref_index=ref_index,
        )
        errors = _validate_actions(actions)
        if errors:
            raise BackendProtocolViolation(errors)
        return actions


class RemoteMockBackend(CoordinatorBackend):
    """Remote coordinator simulator.

    Guarantees:
    - no local file IO assumptions
    - no local path semantics in inputs/actions
    - action-only output
    """

    def __init__(self) -> None:
        super().__init__(name="remote_mock")

    def propose_actions(
        self,
        *,
        snapshot: dict[str, Any],
        recent_event_digests: list[dict[str, Any]],
        control_commands: list[dict[str, Any]],
        ref_index: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        input_errors = _validate_remote_inputs(
            snapshot=snapshot,
            recent_event_digests=recent_event_digests,
            control_commands=control_commands,
            ref_index=ref_index,
        )
        if input_errors:
            raise BackendProtocolViolation(input_errors)

        actions = propose_actions_from_policy(
            snapshot=snapshot,
            recent_event_digests=recent_event_digests,
            control_commands=control_commands,
            ref_index=ref_index,
        )
        output_errors = _validate_remote_actions(actions)
        if output_errors:
            raise BackendProtocolViolation(output_errors)
        return actions


def resolve_backend(name: str) -> CoordinatorBackend:
    normalized = str(name or "local").strip().lower()
    if normalized in {"remote", "remote_mock", "mock"}:
        return RemoteMockBackend()
    return LocalBackend()
