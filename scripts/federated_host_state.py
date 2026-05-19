#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "config" / "federated_host_state_schema.json"

_ROOT_FIELDS = {"schema_version", "host_identity", "static_facts", "runtime_facts", "heartbeat"}
_IDENTITY_FIELDS = {"host_id", "instance_id", "display_name", "host_class", "environment", "role_hint"}
_STATIC_FIELDS = {"device", "resources", "path_scope", "capability_inventory"}
_DEVICE_FIELDS = {"hostname", "os_family", "os_version", "architecture", "device_model"}
_RESOURCES_FIELDS = {"cpu_logical_cores", "memory_total_gb", "storage_roots", "power_profile"}
_STORAGE_ROOT_FIELDS = {"name", "path", "kind", "total_gb", "removable"}
_POWER_PROFILE_FIELDS = {"has_battery", "preferred_power_source"}
_PATH_SCOPE_FIELDS = {"repo_root", "codex_home", "obsidian_vault", "local_model_roots"}
_CAPABILITY_INVENTORY_FIELDS = {"declared_providers", "declared_local_models", "declared_tools", "declared_wrappers"}
_RUNTIME_FIELDS = {"resource_state", "availability", "network"}
_RESOURCE_STATE_FIELDS = {"cpu_load_1m", "memory_available_gb", "storage_free_gb", "power_state"}
_FREE_STORAGE_FIELDS = {"name", "free_gb", "writable"}
_POWER_STATE_FIELDS = {"source", "battery_percent", "charging", "low_power_mode"}
_AVAILABILITY_FIELDS = {"providers", "local_models", "tools", "wrappers"}
_AVAILABILITY_ITEM_FIELDS = {"name", "configured", "available", "reason"}
_NETWORK_FIELDS = {"online", "default_route", "remote_reachability"}
_REMOTE_REACHABILITY_FIELDS = {"target_host_id", "transport", "reachable", "latency_ms", "last_error"}
_HEARTBEAT_FIELDS = {"observed_at", "fresh_until", "stale_after_sec", "collector"}


@dataclass(frozen=True)
class HostStateValidationResult:
    valid: bool
    errors: list[str]


def load_schema() -> dict[str, Any]:
    if not SCHEMA_PATH.exists():
        return {}
    try:
        loaded = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_object(payload: Any, *, path: str, errors: list[str]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        errors.append(f"{path} must be object")
        return {}
    return payload


def _check_unknown_keys(payload: dict[str, Any], *, allowed: set[str], path: str, errors: list[str]) -> None:
    for key in payload:
        if key not in allowed:
            errors.append(f"unknown field: {path}.{key}" if path else f"unknown field: {key}")


def _require_string(payload: dict[str, Any], key: str, *, path: str, errors: list[str], allow_empty: bool = False) -> None:
    value = payload.get(key)
    if not isinstance(value, str):
        errors.append(f"{path}.{key} must be string")
    elif not allow_empty and not value.strip():
        errors.append(f"{path}.{key} must be non-empty string")


def _require_bool(payload: dict[str, Any], key: str, *, path: str, errors: list[str]) -> None:
    if not isinstance(payload.get(key), bool):
        errors.append(f"{path}.{key} must be boolean")


def _require_positive_number(payload: dict[str, Any], key: str, *, path: str, errors: list[str], integer: bool = False) -> None:
    value = payload.get(key)
    if integer:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            errors.append(f"{path}.{key} must be positive integer")
    else:
        if not _is_number(value) or float(value) <= 0:
            errors.append(f"{path}.{key} must be positive number")


def _require_non_negative_number(payload: dict[str, Any], key: str, *, path: str, errors: list[str]) -> None:
    value = payload.get(key)
    if not _is_number(value) or float(value) < 0:
        errors.append(f"{path}.{key} must be non-negative number")


def _require_string_list(payload: dict[str, Any], key: str, *, path: str, errors: list[str]) -> None:
    value = payload.get(key)
    if not isinstance(value, list):
        errors.append(f"{path}.{key} must be array")
        return
    for idx, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{path}.{key}[{idx}] must be non-empty string")


def _validate_iso8601(value: Any, *, path: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{path} must be non-empty string")
        return
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{path} must be ISO-8601 timestamp")


def _validate_availability_list(items: Any, *, path: str, errors: list[str]) -> None:
    if not isinstance(items, list):
        errors.append(f"{path} must be array")
        return
    seen: set[str] = set()
    for idx, item in enumerate(items):
        entry = _require_object(item, path=f"{path}[{idx}]", errors=errors)
        _check_unknown_keys(entry, allowed=_AVAILABILITY_ITEM_FIELDS, path=f"{path}[{idx}]", errors=errors)
        _require_string(entry, "name", path=f"{path}[{idx}]", errors=errors)
        _require_bool(entry, "configured", path=f"{path}[{idx}]", errors=errors)
        _require_bool(entry, "available", path=f"{path}[{idx}]", errors=errors)
        if "reason" in entry and not isinstance(entry.get("reason"), str):
            errors.append(f"{path}[{idx}].reason must be string when provided")
        name = entry.get("name")
        if isinstance(name, str) and name.strip():
            if name in seen:
                errors.append(f"duplicate availability name in {path}: {name}")
            seen.add(name)


def validate_host_state_payload(payload: dict[str, Any]) -> HostStateValidationResult:
    errors: list[str] = []
    root = _require_object(payload, path="payload", errors=errors)
    _check_unknown_keys(root, allowed=_ROOT_FIELDS, path="", errors=errors)

    if root.get("schema_version") != "agn.host_state.v1":
        errors.append("schema_version must equal agn.host_state.v1")

    identity = _require_object(root.get("host_identity"), path="host_identity", errors=errors)
    _check_unknown_keys(identity, allowed=_IDENTITY_FIELDS, path="host_identity", errors=errors)
    for key in ("host_id", "instance_id", "display_name", "host_class", "environment"):
        _require_string(identity, key, path="host_identity", errors=errors)

    static_facts = _require_object(root.get("static_facts"), path="static_facts", errors=errors)
    _check_unknown_keys(static_facts, allowed=_STATIC_FIELDS, path="static_facts", errors=errors)

    device = _require_object(static_facts.get("device"), path="static_facts.device", errors=errors)
    _check_unknown_keys(device, allowed=_DEVICE_FIELDS, path="static_facts.device", errors=errors)
    for key in ("hostname", "os_family", "os_version", "architecture"):
        _require_string(device, key, path="static_facts.device", errors=errors)

    resources = _require_object(static_facts.get("resources"), path="static_facts.resources", errors=errors)
    _check_unknown_keys(resources, allowed=_RESOURCES_FIELDS, path="static_facts.resources", errors=errors)
    _require_positive_number(resources, "cpu_logical_cores", path="static_facts.resources", errors=errors, integer=True)
    _require_positive_number(resources, "memory_total_gb", path="static_facts.resources", errors=errors)
    storage_roots = resources.get("storage_roots")
    if not isinstance(storage_roots, list) or not storage_roots:
        errors.append("static_facts.resources.storage_roots must be non-empty array")
    else:
        for idx, item in enumerate(storage_roots):
            entry = _require_object(item, path=f"static_facts.resources.storage_roots[{idx}]", errors=errors)
            _check_unknown_keys(entry, allowed=_STORAGE_ROOT_FIELDS, path=f"static_facts.resources.storage_roots[{idx}]", errors=errors)
            _require_string(entry, "name", path=f"static_facts.resources.storage_roots[{idx}]", errors=errors)
            _require_string(entry, "path", path=f"static_facts.resources.storage_roots[{idx}]", errors=errors)
            _require_string(entry, "kind", path=f"static_facts.resources.storage_roots[{idx}]", errors=errors)
            _require_positive_number(entry, "total_gb", path=f"static_facts.resources.storage_roots[{idx}]", errors=errors)
            _require_bool(entry, "removable", path=f"static_facts.resources.storage_roots[{idx}]", errors=errors)
    power_profile = _require_object(resources.get("power_profile"), path="static_facts.resources.power_profile", errors=errors)
    _check_unknown_keys(power_profile, allowed=_POWER_PROFILE_FIELDS, path="static_facts.resources.power_profile", errors=errors)
    _require_bool(power_profile, "has_battery", path="static_facts.resources.power_profile", errors=errors)
    _require_string(power_profile, "preferred_power_source", path="static_facts.resources.power_profile", errors=errors)

    path_scope = _require_object(static_facts.get("path_scope"), path="static_facts.path_scope", errors=errors)
    _check_unknown_keys(path_scope, allowed=_PATH_SCOPE_FIELDS, path="static_facts.path_scope", errors=errors)
    for key in ("repo_root", "codex_home"):
        _require_string(path_scope, key, path="static_facts.path_scope", errors=errors)
    if "obsidian_vault" in path_scope and path_scope.get("obsidian_vault") is not None and not isinstance(path_scope.get("obsidian_vault"), str):
        errors.append("static_facts.path_scope.obsidian_vault must be string or null")
    _require_string_list(path_scope, "local_model_roots", path="static_facts.path_scope", errors=errors)

    inventory = _require_object(static_facts.get("capability_inventory"), path="static_facts.capability_inventory", errors=errors)
    _check_unknown_keys(inventory, allowed=_CAPABILITY_INVENTORY_FIELDS, path="static_facts.capability_inventory", errors=errors)
    for key in ("declared_providers", "declared_local_models", "declared_tools", "declared_wrappers"):
        _require_string_list(inventory, key, path="static_facts.capability_inventory", errors=errors)

    runtime_facts = _require_object(root.get("runtime_facts"), path="runtime_facts", errors=errors)
    _check_unknown_keys(runtime_facts, allowed=_RUNTIME_FIELDS, path="runtime_facts", errors=errors)

    resource_state = _require_object(runtime_facts.get("resource_state"), path="runtime_facts.resource_state", errors=errors)
    _check_unknown_keys(resource_state, allowed=_RESOURCE_STATE_FIELDS, path="runtime_facts.resource_state", errors=errors)
    if "cpu_load_1m" in resource_state and resource_state.get("cpu_load_1m") is not None:
        _require_non_negative_number(resource_state, "cpu_load_1m", path="runtime_facts.resource_state", errors=errors)
    _require_non_negative_number(resource_state, "memory_available_gb", path="runtime_facts.resource_state", errors=errors)
    free_storage = resource_state.get("storage_free_gb")
    if not isinstance(free_storage, list):
        errors.append("runtime_facts.resource_state.storage_free_gb must be array")
    else:
        for idx, item in enumerate(free_storage):
            entry = _require_object(item, path=f"runtime_facts.resource_state.storage_free_gb[{idx}]", errors=errors)
            _check_unknown_keys(entry, allowed=_FREE_STORAGE_FIELDS, path=f"runtime_facts.resource_state.storage_free_gb[{idx}]", errors=errors)
            _require_string(entry, "name", path=f"runtime_facts.resource_state.storage_free_gb[{idx}]", errors=errors)
            _require_non_negative_number(entry, "free_gb", path=f"runtime_facts.resource_state.storage_free_gb[{idx}]", errors=errors)
            _require_bool(entry, "writable", path=f"runtime_facts.resource_state.storage_free_gb[{idx}]", errors=errors)
    power_state = _require_object(resource_state.get("power_state"), path="runtime_facts.resource_state.power_state", errors=errors)
    _check_unknown_keys(power_state, allowed=_POWER_STATE_FIELDS, path="runtime_facts.resource_state.power_state", errors=errors)
    _require_string(power_state, "source", path="runtime_facts.resource_state.power_state", errors=errors)
    for key in ("battery_percent", "charging", "low_power_mode"):
        value = power_state.get(key)
        if key == "battery_percent" and value is not None:
            if not _is_number(value) or float(value) < 0 or float(value) > 100:
                errors.append("runtime_facts.resource_state.power_state.battery_percent must be 0-100 or null")
        elif key in {"charging", "low_power_mode"} and value is not None and not isinstance(value, bool):
            errors.append(f"runtime_facts.resource_state.power_state.{key} must be boolean or null")

    availability = _require_object(runtime_facts.get("availability"), path="runtime_facts.availability", errors=errors)
    _check_unknown_keys(availability, allowed=_AVAILABILITY_FIELDS, path="runtime_facts.availability", errors=errors)
    for key in ("providers", "local_models", "tools", "wrappers"):
        _validate_availability_list(availability.get(key), path=f"runtime_facts.availability.{key}", errors=errors)

    network = _require_object(runtime_facts.get("network"), path="runtime_facts.network", errors=errors)
    _check_unknown_keys(network, allowed=_NETWORK_FIELDS, path="runtime_facts.network", errors=errors)
    _require_bool(network, "online", path="runtime_facts.network", errors=errors)
    if "default_route" in network and network.get("default_route") is not None and not isinstance(network.get("default_route"), bool):
        errors.append("runtime_facts.network.default_route must be boolean or null")
    remote_reachability = network.get("remote_reachability")
    if not isinstance(remote_reachability, list):
        errors.append("runtime_facts.network.remote_reachability must be array")
    else:
        for idx, item in enumerate(remote_reachability):
            entry = _require_object(item, path=f"runtime_facts.network.remote_reachability[{idx}]", errors=errors)
            _check_unknown_keys(entry, allowed=_REMOTE_REACHABILITY_FIELDS, path=f"runtime_facts.network.remote_reachability[{idx}]", errors=errors)
            for key in ("target_host_id", "transport"):
                _require_string(entry, key, path=f"runtime_facts.network.remote_reachability[{idx}]", errors=errors)
            _require_bool(entry, "reachable", path=f"runtime_facts.network.remote_reachability[{idx}]", errors=errors)
            if "latency_ms" in entry and entry.get("latency_ms") is not None:
                _require_non_negative_number(entry, "latency_ms", path=f"runtime_facts.network.remote_reachability[{idx}]", errors=errors)
            if "last_error" in entry and not isinstance(entry.get("last_error"), str):
                errors.append(f"runtime_facts.network.remote_reachability[{idx}].last_error must be string when provided")

    heartbeat = _require_object(root.get("heartbeat"), path="heartbeat", errors=errors)
    _check_unknown_keys(heartbeat, allowed=_HEARTBEAT_FIELDS, path="heartbeat", errors=errors)
    _validate_iso8601(heartbeat.get("observed_at"), path="heartbeat.observed_at", errors=errors)
    _validate_iso8601(heartbeat.get("fresh_until"), path="heartbeat.fresh_until", errors=errors)
    _require_positive_number(heartbeat, "stale_after_sec", path="heartbeat", errors=errors, integer=True)
    _require_string(heartbeat, "collector", path="heartbeat", errors=errors)

    return HostStateValidationResult(valid=not errors, errors=errors)
