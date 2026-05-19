"""AGN Command Request.

Structured admin-approval queue for controlled utility operations.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

try:
    from agn.core.guarded_io import atomic_write_json
except ImportError:  # pragma: no cover - package import fallback
    from scripts.guarded_io import atomic_write_json  # type: ignore[no-redef]

COMMAND_REQUESTS_DIR = ROOT / "dispatch" / "command_requests"
AUDIT_PATH = ROOT / "audit" / "events.jsonl"
UTILITY_SANDBOX_DIR = ROOT / ".agn_workspace" / "utility_ops"

ALLOWED_OPERATIONS = {"git_clone", "git_checkout", "fetch_remote"}
VALID_STATUSES = {"pending", "approved", "rejected", "executed"}


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _allowed_repo_hosts() -> set[str]:
    raw = str(os.environ.get("AGN_COMMAND_REQUEST_ALLOWED_HOSTS", "")).strip()
    if not raw:
        return {"github.com"}
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _append_audit(action: str, **extra: Any) -> None:
    event = {
        "timestamp": _utc_now_iso(),
        "route": "/agn/command_request",
        "status": 200,
        "action": action,
        **extra,
    }
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=True) + "\n")


def _request_path(request_id: str) -> Path:
    return COMMAND_REQUESTS_DIR / f"{request_id}.json"


def _save_request(request_id: str, payload: dict[str, Any]) -> None:
    COMMAND_REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(_request_path(request_id), payload)


def _resolve_sandbox_dir(raw: str) -> Path:
    target = Path(raw).expanduser()
    if not target.is_absolute():
        target = UTILITY_SANDBOX_DIR / target
    resolved = target.resolve(strict=False)
    sandbox = UTILITY_SANDBOX_DIR.resolve(strict=False)
    if os.path.commonpath([str(resolved), str(sandbox)]) != str(sandbox):
        raise ValueError(f"target path escapes utility sandbox: {resolved}")
    return resolved


def _validate_repo_url(repo_url: str) -> str:
    parsed = urlparse(repo_url)
    if parsed.scheme != "https":
        raise ValueError("repo_url must use https")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("repo_url missing hostname")
    if host not in _allowed_repo_hosts():
        raise ValueError(f"repo_url host not allowed: {host}")
    return repo_url


def _normalize_legacy_command(command: list[str]) -> tuple[str, dict[str, Any]]:
    if not command:
        raise ValueError("legacy command is empty")
    head = command[0]
    if head != "git":
        raise ValueError("legacy command must be git")

    if len(command) >= 3 and command[1] == "clone":
        params: dict[str, Any] = {"repo_url": command[2]}
        if len(command) >= 4:
            params["target_dir"] = command[3]
        return "git_clone", params

    raise ValueError("unsupported legacy command; use structured operation payload")


def _normalize_operation(
    *,
    operation: str | None,
    params: dict[str, Any] | None,
    command: list[str] | None,
) -> tuple[str, dict[str, Any]]:
    if operation:
        op = operation.strip().lower()
        if op not in ALLOWED_OPERATIONS:
            raise ValueError(f"unsupported operation: {operation}")
        return op, dict(params or {})
    if command is not None:
        return _normalize_legacy_command(command)
    raise ValueError("operation is required")


def _validate_operation_payload(operation: str, params: dict[str, Any]) -> dict[str, Any]:
    if operation == "git_clone":
        repo_url = _validate_repo_url(str(params.get("repo_url", "")).strip())
        raw_target = str(params.get("target_dir", "")).strip() or "repos"
        target_dir = _resolve_sandbox_dir(raw_target)
        normalized: dict[str, Any] = {
            "repo_url": repo_url,
            "target_dir": str(target_dir),
        }
        branch = str(params.get("branch", "")).strip()
        if branch:
            normalized["branch"] = branch
        depth_raw = params.get("depth")
        if depth_raw is not None and str(depth_raw).strip():
            depth = int(depth_raw)
            if depth <= 0:
                raise ValueError("depth must be positive")
            normalized["depth"] = depth
        return normalized

    if operation == "git_checkout":
        repo_dir = _resolve_sandbox_dir(str(params.get("repo_dir", "")).strip())
        ref = str(params.get("ref", "")).strip()
        if not ref:
            raise ValueError("git_checkout requires ref")
        return {"repo_dir": str(repo_dir), "ref": ref}

    if operation == "fetch_remote":
        repo_dir = _resolve_sandbox_dir(str(params.get("repo_dir", "")).strip())
        remote = str(params.get("remote", "origin")).strip() or "origin"
        prune = bool(params.get("prune", True))
        return {"repo_dir": str(repo_dir), "remote": remote, "prune": prune}

    raise ValueError(f"unsupported operation: {operation}")


def submit_request(
    *,
    operation: str | None = None,
    params: dict[str, Any] | None = None,
    command: list[str] | None = None,
    task_id: str | None = None,
    reason: str = "",
    requested_by_role: str = "coordinator",
) -> dict[str, Any]:
    """Write a new structured command request and return payload."""
    op, op_params = _normalize_operation(operation=operation, params=params, command=command)
    clean_params = _validate_operation_payload(op, op_params)

    request_id = f"cr-{uuid4().hex[:12]}"
    payload: dict[str, Any] = {
        "request_id": request_id,
        "task_id": task_id,
        "requested_by_role": requested_by_role,
        "operation": op,
        "params": clean_params,
        "reason": reason[:500],
        "status": "pending",
        "created_at": _utc_now_iso(),
        "approved_by": None,
        "approved_at": None,
        "rejected_by": None,
        "rejected_at": None,
        "executed_by": None,
        "executed_at": None,
        "execution_rc": None,
        "execution_summary": None,
    }
    _save_request(request_id, payload)
    _append_audit(
        "command_request_submitted",
        request_id=request_id,
        operation=op,
        task_id=task_id,
        requested_by_role=requested_by_role,
    )
    return payload


def load_request(request_id: str) -> dict[str, Any] | None:
    path = _request_path(request_id)
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded


def _transition_request(payload: dict[str, Any], *, expected: str, target: str) -> bool:
    current = str(payload.get("status", "")).strip().lower()
    if current not in VALID_STATUSES:
        return False
    if current != expected:
        return False
    payload["status"] = target
    return True


def approve_request(request_id: str, approved_by: str = "admin") -> dict[str, Any] | None:
    payload = load_request(request_id)
    if payload is None:
        return None
    if not _transition_request(payload, expected="pending", target="approved"):
        return payload
    payload["approved_by"] = approved_by
    payload["approved_at"] = _utc_now_iso()
    _save_request(request_id, payload)
    _append_audit("command_request_approved", request_id=request_id, approved_by=approved_by, task_id=payload.get("task_id"))
    return payload


def reject_request(request_id: str, rejected_by: str = "admin") -> dict[str, Any] | None:
    payload = load_request(request_id)
    if payload is None:
        return None
    if not _transition_request(payload, expected="pending", target="rejected"):
        return payload
    payload["rejected_by"] = rejected_by
    payload["rejected_at"] = _utc_now_iso()
    _save_request(request_id, payload)
    _append_audit("command_request_rejected", request_id=request_id, rejected_by=rejected_by, task_id=payload.get("task_id"))
    return payload


def _build_execution_plan(payload: dict[str, Any]) -> tuple[list[str], Path]:
    operation = str(payload.get("operation", "")).strip().lower()
    params = payload.get("params")
    if not isinstance(params, dict):
        raise ValueError("request params must be object")

    clean_params = _validate_operation_payload(operation, params)
    if operation == "git_clone":
        target_dir = Path(str(clean_params["target_dir"]))
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        cmd: list[str] = ["git", "clone"]
        depth = clean_params.get("depth")
        if isinstance(depth, int) and depth > 0:
            cmd.extend(["--depth", str(depth)])
        branch = str(clean_params.get("branch", "")).strip()
        if branch:
            cmd.extend(["--branch", branch])
        cmd.extend([str(clean_params["repo_url"]), str(target_dir)])
        return cmd, UTILITY_SANDBOX_DIR

    if operation == "git_checkout":
        repo_dir = Path(str(clean_params["repo_dir"]))
        ref = str(clean_params["ref"])
        cmd = ["git", "-C", str(repo_dir), "checkout", ref]
        return cmd, repo_dir

    if operation == "fetch_remote":
        repo_dir = Path(str(clean_params["repo_dir"]))
        remote = str(clean_params.get("remote", "origin"))
        cmd = ["git", "-C", str(repo_dir), "fetch", remote]
        if bool(clean_params.get("prune", True)):
            cmd.append("--prune")
        return cmd, repo_dir

    raise ValueError(f"unsupported operation: {operation}")


def execute_approved_requests(*, cwd: Path | None = None, executed_by: str = "admin") -> list[dict[str, Any]]:
    """Execute approved requests exactly once (idempotent)."""
    if not COMMAND_REQUESTS_DIR.exists():
        return []
    executed: list[dict[str, Any]] = []
    UTILITY_SANDBOX_DIR.mkdir(parents=True, exist_ok=True)

    for path in sorted(COMMAND_REQUESTS_DIR.glob("cr-*.json")):
        payload = load_request(path.stem)
        if payload is None:
            continue
        if str(payload.get("status", "")).strip().lower() != "approved":
            continue
        if payload.get("executed_at"):
            continue

        try:
            cmd, cmd_cwd = _build_execution_plan(payload)
            proc = subprocess.run(
                cmd,
                cwd=str(cwd or cmd_cwd),
                text=True,
                capture_output=True,
                timeout=300,
                env={
                    **os.environ,
                    "AGN_ROLE": "admin",
                    "AGN_RUNTIME_CONTEXT": "agn_network",
                    "AGN_ENFORCE_ROLE_GUARD": "1",
                },
            )
            payload["execution_rc"] = int(proc.returncode)
            payload["execution_summary"] = (proc.stderr or proc.stdout or "")[:500]
        except subprocess.TimeoutExpired:
            payload["execution_rc"] = 124
            payload["execution_summary"] = "TIMEOUT_EXPIRED"
        except Exception as exc:
            payload["execution_rc"] = 1
            payload["execution_summary"] = f"{type(exc).__name__}:{str(exc)[:350]}"

        payload["status"] = "executed"
        payload["executed_at"] = _utc_now_iso()
        payload["executed_by"] = executed_by
        _save_request(str(payload.get("request_id", "")), payload)
        _append_audit(
            "command_request_executed",
            request_id=payload.get("request_id"),
            task_id=payload.get("task_id"),
            operation=payload.get("operation"),
            execution_rc=payload.get("execution_rc"),
            executed_by=executed_by,
        )
        executed.append(payload)
    return executed


def list_pending_requests() -> list[dict[str, Any]]:
    if not COMMAND_REQUESTS_DIR.exists():
        return []
    results: list[dict[str, Any]] = []
    for path in sorted(COMMAND_REQUESTS_DIR.glob("cr-*.json")):
        payload = load_request(path.stem)
        if payload is None:
            continue
        if str(payload.get("status", "")).strip().lower() == "pending":
            results.append(payload)
    return results
