"""AGN emergency-stop state and integrity helpers.

This is the real package implementation for AGN's signed system-mode state,
emergency-stop activation/release, and related runtime posture helpers.
The legacy script remains only as a compatibility shim.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

try:
    from agn.core.admin_control import (
        append_admin_audit,
        atomic_write_json,
        ensure_admin_dirs,
        load_json,
        repo_root,
        system_mode_path,
        utc_now_iso,
    )
    from agn.core.constitution import emergency_stop_policy
except ImportError:  # pragma: no cover
    from agn.core.admin_control import (
        append_admin_audit,
        atomic_write_json,
        ensure_admin_dirs,
        load_json,
        repo_root,
        system_mode_path,
        utc_now_iso,
    )
    from agn.core.constitution import emergency_stop_policy


PACKAGE_PATH = "agn.core.emergency_stop"
LEGACY_SCRIPT_SHIM = "scripts/emergency_stop.py"
DEFAULT_MODE: dict[str, Any] = {
    "updated_at": "",
    "mode": "normal",
    "emergency_stop_active": False,
    "dispatcher_accepts_new_work": True,
    "desktop_mode": "normal",
    "external_reviewers_paused": False,
    "release_required": False,
    "last_changed_by": "",
    "last_reason": "",
    "trace_id": "",
}


def _integrity_key() -> bytes:
    constitution_path = repo_root() / "agn2" / "governance" / "constitution.json"
    try:
        content = constitution_path.read_bytes()
    except Exception:
        content = b"fallback-constitution-missing"
    return hashlib.sha256(b"agn2-integrity-" + content).digest()


def _sign_payload(payload: dict[str, Any]) -> dict[str, Any]:
    import copy

    signed = copy.deepcopy(payload)
    signed.pop("_integrity_sig", None)
    canonical = json.dumps(signed, sort_keys=True, ensure_ascii=True)
    sig = hmac.new(_integrity_key(), canonical.encode(), hashlib.sha256).hexdigest()
    signed["_integrity_sig"] = sig
    return signed


def _verify_payload(payload: dict[str, Any]) -> bool:
    sig = payload.get("_integrity_sig")
    if not sig:
        return False
    import copy

    check = copy.deepcopy(payload)
    check.pop("_integrity_sig", None)
    canonical = json.dumps(check, sort_keys=True, ensure_ascii=True)
    expected = hmac.new(_integrity_key(), canonical.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(str(sig), expected)


def _fail_closed_mode(reason: str) -> dict[str, Any]:
    safe = dict(DEFAULT_MODE)
    safe["mode"] = reason
    safe["emergency_stop_active"] = True
    safe["dispatcher_accepts_new_work"] = False
    safe["desktop_mode"] = "observe_only"
    safe["external_reviewers_paused"] = True
    return safe


def load_system_mode() -> dict[str, Any]:
    mode_path = system_mode_path()
    if not mode_path.exists():
        # If admin dir exists but system_mode.json is missing, the file was
        # deleted after initialization — fail closed to prevent silent bypass.
        if mode_path.parent.exists():
            import sys

            print("[emergency_stop] system_mode.json missing from initialized system — failing closed.", file=sys.stderr)
            return _fail_closed_mode("missing_system_mode")
        # Fresh environment with no admin dirs — allow bootstrap.
        return dict(DEFAULT_MODE)
    payload = load_json(mode_path, default=DEFAULT_MODE)
    if not payload:
        return _fail_closed_mode("empty_system_mode")
    merged = dict(DEFAULT_MODE)
    merged.update(payload)
    if "_integrity_sig" in merged:
        if not _verify_payload(merged):
            import sys

            print("[emergency_stop] INTEGRITY CHECK FAILED — possible tampering detected. Failing closed.", file=sys.stderr)
            return _fail_closed_mode("integrity_violation")
        merged.pop("_integrity_sig", None)
    return merged


def _write_mode(payload: dict[str, Any]) -> dict[str, Any]:
    ensure_admin_dirs()
    signed = _sign_payload(payload)
    atomic_write_json(system_mode_path(), signed)
    return payload


def _bootstrap_write_mode(payload: dict[str, Any]) -> dict[str, Any]:
    nonce_path = system_mode_path().parent / ".override_nonce"
    old_override = os.environ.get("AGN_ADMIN_OVERRIDE")
    old_nonce_path = os.environ.get("AGN_OVERRIDE_NONCE_PATH")
    nonce = uuid4().hex
    nonce_path.parent.mkdir(parents=True, exist_ok=True)
    nonce_path.write_text(nonce, encoding="utf-8")
    os.environ["AGN_ADMIN_OVERRIDE"] = nonce
    os.environ.pop("AGN_OVERRIDE_NONCE_PATH", None)
    try:
        return _write_mode(payload)
    finally:
        if old_override is None:
            os.environ.pop("AGN_ADMIN_OVERRIDE", None)
        else:
            os.environ["AGN_ADMIN_OVERRIDE"] = old_override
        if old_nonce_path is None:
            os.environ.pop("AGN_OVERRIDE_NONCE_PATH", None)
        else:
            os.environ["AGN_OVERRIDE_NONCE_PATH"] = old_nonce_path
        nonce_path.unlink(missing_ok=True)


def initialize_system_mode(*, issuer: str, reason: str, trace_id: str = "") -> dict[str, Any]:
    current = load_json(system_mode_path(), default={})
    if isinstance(current, dict) and current:
        payload = dict(DEFAULT_MODE)
        payload.update(current)
        payload.pop("_integrity_sig", None)
        return payload
    payload = {
        "updated_at": utc_now_iso(),
        "mode": "normal",
        "emergency_stop_active": False,
        "dispatcher_accepts_new_work": True,
        "desktop_mode": "normal",
        "external_reviewers_paused": False,
        "release_required": False,
        "last_changed_by": str(issuer).strip(),
        "last_reason": str(reason).strip(),
        "trace_id": str(trace_id).strip(),
    }
    append_admin_audit("system_mode_initialized", issuer=issuer, reason=reason, trace_id=trace_id)
    return _bootstrap_write_mode(payload)


def activate_emergency_stop(*, issuer: str, reason: str, trace_id: str = "") -> dict[str, Any]:
    policy = emergency_stop_policy()
    payload = {
        "updated_at": utc_now_iso(),
        "mode": "emergency_stop",
        "emergency_stop_active": True,
        "dispatcher_accepts_new_work": bool(policy.get("dispatcher_accepts_new_work", False)),
        "desktop_mode": str(policy.get("desktop_mode", "observe_only")).strip() or "observe_only",
        "external_reviewers_paused": bool(policy.get("external_reviewers_paused", True)),
        "release_required": True,
        "last_changed_by": str(issuer).strip(),
        "last_reason": str(reason).strip(),
        "trace_id": str(trace_id).strip(),
    }
    append_admin_audit("emergency_stop_activated", issuer=issuer, reason=reason, trace_id=trace_id)
    return _write_mode(payload)


def release_emergency_stop(*, issuer: str, reason: str, trace_id: str = "") -> dict[str, Any]:
    payload = {
        "updated_at": utc_now_iso(),
        "mode": "normal",
        "emergency_stop_active": False,
        "dispatcher_accepts_new_work": True,
        "desktop_mode": "normal",
        "external_reviewers_paused": False,
        "release_required": False,
        "last_changed_by": str(issuer).strip(),
        "last_reason": str(reason).strip(),
        "trace_id": str(trace_id).strip(),
    }
    append_admin_audit("emergency_stop_released", issuer=issuer, reason=reason, trace_id=trace_id)
    return _write_mode(payload)


def is_emergency_stop_active() -> bool:
    return bool(load_system_mode().get("emergency_stop_active", False))


def dispatcher_accepts_new_work() -> bool:
    payload = load_system_mode()
    return bool(payload.get("dispatcher_accepts_new_work", False)) and not bool(payload.get("emergency_stop_active", False))


def desktop_mode() -> str:
    return str(load_system_mode().get("desktop_mode", "normal")).strip() or "normal"


def external_reviewers_paused() -> bool:
    return bool(load_system_mode().get("external_reviewers_paused", False))
