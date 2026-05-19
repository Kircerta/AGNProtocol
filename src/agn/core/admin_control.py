"""AGN admin-control common utilities.

This is the real package implementation for AGN's shared admin-control path,
JSON I/O, governance path, and constitution-protected write helpers.
The legacy script remains only as a compatibility shim.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


PACKAGE_PATH = "agn.core.admin_control"
LEGACY_SCRIPT_SHIM = "scripts/admin_control_common.py"
DEFAULT_ROOT = Path(__file__).resolve().parents[3]


def repo_root() -> Path:
    override = str(os.getenv("AGN_REPO_ROOT", "")).strip()
    if override:
        return Path(override).expanduser().resolve()
    return DEFAULT_ROOT


def governance_root() -> Path:
    return repo_root() / "agn2" / "governance"


def agn2_root() -> Path:
    return repo_root() / "agn2"


def control_plane_root() -> Path:
    return agn2_root() / "control_plane"


def lifecycle_root() -> Path:
    return admin_root() / "lifecycle"


def lifecycle_state_path() -> Path:
    return lifecycle_root() / "agn2_system.json"


def admin_root() -> Path:
    return repo_root() / "runtime" / "admin_control"


def command_pending_dir() -> Path:
    return admin_root() / "commands" / "pending"


def command_done_dir() -> Path:
    return admin_root() / "commands" / "done"


def command_failed_dir() -> Path:
    return admin_root() / "commands" / "failed"


def command_acks_dir() -> Path:
    return admin_root() / "commands" / "acks"


def command_index_path() -> Path:
    return admin_root() / "commands" / "index.jsonl"


def policy_gate_queue_dir() -> Path:
    return admin_root() / "policy_gate" / "queue"


def policy_gate_decisions_dir() -> Path:
    return admin_root() / "policy_gate" / "decisions"


def policy_gate_index_path() -> Path:
    return admin_root() / "policy_gate" / "index.jsonl"


def council_cases_dir() -> Path:
    return admin_root() / "council" / "cases"


def council_verdicts_dir() -> Path:
    return admin_root() / "council" / "verdicts"


def council_index_path() -> Path:
    return admin_root() / "council" / "index.jsonl"


def read_models_dir() -> Path:
    return admin_root() / "read_models"


def audit_dir() -> Path:
    return admin_root() / "audit"


def admin_audit_path() -> Path:
    return audit_dir() / "admin_control.jsonl"


def system_mode_path() -> Path:
    return admin_root() / "system_mode.json"


def isolated_agents_path() -> Path:
    return admin_root() / "agents" / "isolated.json"


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def safe_name(value: str, *, default: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(value or "").strip())
    return normalized[:160] or default


def ensure_admin_dirs() -> None:
    for path in (
        command_pending_dir(),
        command_done_dir(),
        command_failed_dir(),
        command_acks_dir(),
        policy_gate_queue_dir(),
        policy_gate_decisions_dir(),
        council_cases_dir(),
        council_verdicts_dir(),
        read_models_dir(),
        audit_dir(),
        lifecycle_root(),
        isolated_agents_path().parent,
    ):
        path.mkdir(parents=True, exist_ok=True)


_CONSTITUTION_PROTECTED: frozenset[str] | None = None
_CONSTITUTION_PROTECTED_ROOT: str | None = None


def _resolve_protected_paths() -> frozenset[str]:
    global _CONSTITUTION_PROTECTED, _CONSTITUTION_PROTECTED_ROOT
    root = repo_root()
    root_key = str(root.resolve())
    if _CONSTITUTION_PROTECTED is not None and _CONSTITUTION_PROTECTED_ROOT == root_key:
        return _CONSTITUTION_PROTECTED
    gov_path = governance_root() / "constitution.json"
    protected_relative = [
        "agn2/governance/constitution.json",
        "agn2/governance/policy_gate.json",
        "runtime/admin_control/system_mode.json",
    ]
    if gov_path.exists():
        try:
            raw_payload = json.loads(gov_path.read_text(encoding="utf-8"))
            immut = raw_payload.get("immutability", {})
            from_file = immut.get("agent_may_not_modify", [])
            if isinstance(from_file, list) and from_file:
                protected_relative = [str(item).strip() for item in from_file if str(item).strip()]
        except Exception:
            pass
    _CONSTITUTION_PROTECTED = frozenset(str((root / p).resolve()) for p in protected_relative)
    _CONSTITUTION_PROTECTED_ROOT = root_key
    return _CONSTITUTION_PROTECTED


def _guard_constitution_protected(path: Path) -> None:
    resolved = str(path.resolve())
    if resolved in _resolve_protected_paths():
        override_val = str(os.getenv("AGN_ADMIN_OVERRIDE", "")).strip()
        if not override_val:
            raise ValueError(
                f"constitution_immutability_violation: writing to {path} "
                f"is blocked by agn2/governance/constitution.json "
                f"(agent_may_not_modify). Set AGN_ADMIN_OVERRIDE via "
                f"control_daemon to override."
            )
        default_nonce_path = (repo_root() / "runtime" / "admin_control" / ".override_nonce").resolve()
        nonce_env = os.getenv("AGN_OVERRIDE_NONCE_PATH", "").strip()
        nonce_path = Path(nonce_env).expanduser().resolve() if nonce_env else default_nonce_path
        if nonce_path != default_nonce_path:
            raise ValueError(
                "constitution_immutability_violation: override nonce path must "
                "remain on the canonical admin-control nonce file."
            )
        if not nonce_path.exists():
            raise ValueError(
                "constitution_immutability_violation: admin override nonce file "
                "is missing; refuse fail-open constitution writes."
            )
        expected = nonce_path.read_text(encoding="utf-8").strip()
        if not expected:
            raise ValueError(
                "constitution_immutability_violation: admin override nonce file "
                "is empty; refuse fail-open constitution writes."
            )
        if override_val != expected:
            raise ValueError(
                "constitution_immutability_violation: AGN_ADMIN_OVERRIDE "
                "value does not match current nonce. Stale or spoofed override."
            )


def atomic_write_text(path: Path, content: str) -> None:
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


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _guard_constitution_protected(path)
    atomic_write_text(path, json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_json(path: Path, *, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return dict(default or {})
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(default or {})


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def append_admin_audit(event_type: str, **fields: Any) -> dict[str, Any]:
    ensure_admin_dirs()
    payload = {
        "ts": utc_now_iso(),
        "event_type": event_type,
        **fields,
    }
    append_jsonl(admin_audit_path(), payload)
    return payload
