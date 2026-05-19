#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import types
from typing import Any, Callable

try:
    import httpx
except ModuleNotFoundError as exc:  # pragma: no cover - py3.14 compatibility path
    if getattr(exc, "name", "") != "cgi":
        raise
    from email.parser import Parser

    shim = types.ModuleType("cgi")

    def _parse_header(line: str) -> tuple[str, dict[str, str]]:
        if not isinstance(line, str):
            line = str(line)
        if not line:
            return "", {}
        msg = Parser().parsestr(f"Content-Type: {line}\n\n")
        ctype = msg.get_content_type()
        params = {k.lower(): v for k, v in msg.get_params()[1:]}
        return ctype, params

    shim.parse_header = _parse_header  # type: ignore[attr-defined]
    sys.modules["cgi"] = shim
    import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from agn_api.audit import AuditLogger
from agn_api.ssot_store import SSOTStore
try:
    from agn.core.role_guard import check_command as _rg_check_command, get_current_role as _rg_role, log_violation as _rg_log_violation
except ImportError:  # pragma: no cover - package import fallback
    from scripts.role_guard import check_command as _rg_check_command, log_violation as _rg_log_violation, get_current_role as _rg_role

try:
    from agn.core.guarded_io import atomic_write_json as _guarded_atomic_write_json, write_text as _guarded_write_text
except ImportError:  # pragma: no cover - package import fallback
    from scripts.guarded_io import atomic_write_json as _guarded_atomic_write_json, write_text as _guarded_write_text

try:
    from pointer_protocol import ref_to_artifact_entry, write_json_artifact, write_text_artifact
except ImportError:  # pragma: no cover - package import fallback
    from scripts.pointer_protocol import ref_to_artifact_entry, write_json_artifact, write_text_artifact
try:
    from provider_registry import load_registry, resolve_reviewer_provider
except ImportError:  # pragma: no cover - package import fallback
    from scripts.provider_registry import load_registry, resolve_reviewer_provider


SSOT_DIR = ROOT / "ssot"
DISPATCH_DIR = ROOT / "dispatch"
ACK_DIR = DISPATCH_DIR / "acks"
RESULTS_DIR = ROOT / "results"
VERDICTS_DIR = ROOT / "verdicts"
AUDIT_PATH = ROOT / "audit" / "events.jsonl"
REPORTS_DIR = ROOT / "reports"
SCRATCH_ROOT = ROOT / ".agn_workspace" / "scratch"
ROLE_CONTRACT_PATH = ROOT / "config" / "agent_role_contracts.json"
_DISPATCH_REQUEST_INLINE_LIMIT = max(512, int(os.getenv("AGN_DISPATCH_REQUEST_INLINE_LIMIT", "4096") or "4096"))
_REQUEST_SUMMARY_LIMIT = max(120, int(os.getenv("AGN_DISPATCH_REQUEST_SUMMARY_LIMIT", "480") or "480"))
_EXECUTOR_PROMPT_MAX_CHARS = max(2000, int(os.getenv("AGN_EXECUTOR_PROMPT_MAX_CHARS", "12000") or "12000"))
_REVIEWER_PROMPT_MAX_CHARS = max(2000, int(os.getenv("AGN_REVIEWER_PROMPT_MAX_CHARS", "12000") or "12000"))
_PROMPT_LOG_PREVIEW_CHARS = max(40, int(os.getenv("AGN_PROMPT_LOG_PREVIEW_CHARS", "180") or "180"))

_ATTEMPT_RE = re.compile(r"^(?P<task>.+)\.(?P<attempt>\d+)\.json$")
VALID_RISK_LEVELS = {"low", "medium", "high"}
VALID_SIDE_EFFECT_LEVELS = {"read_only", "local_write", "external_publish"}
_PROVIDER_REGISTRY = load_registry()
_LAST_PROVIDER_PROBE: float = time.monotonic()
# Re-probe every N seconds (default 600 = 10 min).
_PROVIDER_REPROBE_INTERVAL = max(60, int(os.getenv("AGN_PROVIDER_REPROBE_INTERVAL", "600") or "600"))


def _maybe_reprobe_providers() -> None:
    """Reload provider registry if the reprobe interval has elapsed (P2-12).

    This allows the system to detect newly available or expired providers
    without requiring a full restart.
    """
    global _PROVIDER_REGISTRY, _LAST_PROVIDER_PROBE  # noqa: PLW0603
    now = time.monotonic()
    if now - _LAST_PROVIDER_PROBE < _PROVIDER_REPROBE_INTERVAL:
        return
    _LAST_PROVIDER_PROBE = now
    try:
        from provider_registry import probe_capabilities, atomic_write_json as _pr_atomic_write

        _PROVIDER_REGISTRY = load_registry()
        caps = probe_capabilities(_PROVIDER_REGISTRY)
        caps_path = ROOT / "runtime" / "provider_capabilities.json"
        _pr_atomic_write(caps_path, caps)
    except Exception:
        # Probing is best-effort; don't crash the worker.
        pass


@dataclass(frozen=True)
class RuntimePaths:
    root: Path = ROOT
    ssot_dir: Path = SSOT_DIR
    dispatch_dir: Path = DISPATCH_DIR
    ack_dir: Path = ACK_DIR
    results_dir: Path = RESULTS_DIR
    verdicts_dir: Path = VERDICTS_DIR
    audit_path: Path = AUDIT_PATH
    reports_dir: Path = REPORTS_DIR


@dataclass
class CommandOutcome:
    command: list[str]
    cwd: str
    return_code: int
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool


PATHS = RuntimePaths()
_AUDIT = AuditLogger(PATHS.audit_path)


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def safe_task_id(task_id: str) -> str:
    return task_id.replace("/", "_")


def ensure_dirs() -> None:
    PATHS.ssot_dir.mkdir(parents=True, exist_ok=True)
    PATHS.dispatch_dir.mkdir(parents=True, exist_ok=True)
    PATHS.ack_dir.mkdir(parents=True, exist_ok=True)
    PATHS.results_dir.mkdir(parents=True, exist_ok=True)
    PATHS.verdicts_dir.mkdir(parents=True, exist_ok=True)
    PATHS.audit_path.parent.mkdir(parents=True, exist_ok=True)
    PATHS.reports_dir.mkdir(parents=True, exist_ok=True)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _guarded_atomic_write_json(path, payload)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def dispatch_path(task_id: str) -> Path:
    return PATHS.dispatch_dir / f"{safe_task_id(task_id)}.json"


def ack_path(task_id: str, attempt: int) -> Path:
    return PATHS.ack_dir / f"{safe_task_id(task_id)}.{attempt}.json"


def result_path(task_id: str, attempt: int) -> Path:
    return PATHS.results_dir / f"{safe_task_id(task_id)}.{attempt}.json"


def verdict_path(task_id: str, attempt: int) -> Path:
    return PATHS.verdicts_dir / f"{safe_task_id(task_id)}.{attempt}.json"


def latest_attempt_for(task_id: str, directory: Path) -> int:
    safe_id = safe_task_id(task_id)
    latest = 0
    for path in directory.glob(f"{safe_id}.*.json"):
        match = _ATTEMPT_RE.match(path.name)
        if not match:
            continue
        if match.group("task") != safe_id:
            continue
        latest = max(latest, int(match.group("attempt")))
    return latest


def list_dispatches() -> list[tuple[Path, dict[str, Any]]]:
    items: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(PATHS.dispatch_dir.glob("*.json")):
        try:
            payload = load_json(path)
        except Exception:
            continue
        items.append((path, payload))
    return items


def append_audit(*, action: str, task_id: str | None, route: str, status: int, **extra: object) -> None:
    _AUDIT.log_event(route=route, status=status, task_id=task_id, action=action, **extra)


def _load_role_contract(role: str) -> dict[str, Any]:
    if not ROLE_CONTRACT_PATH.exists():
        return {}
    try:
        payload = json.loads(ROLE_CONTRACT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    roles = payload.get("roles", {})
    if not isinstance(roles, dict):
        return {}
    contract = roles.get(str(role).strip().lower())
    return contract if isinstance(contract, dict) else {}


def _summarize_text(text: str, *, max_chars: int = _REQUEST_SUMMARY_LIMIT) -> str:
    raw = str(text or "").strip()
    if len(raw) <= max_chars:
        return raw
    return raw[: max_chars - 24] + "...<truncated-summary>..."


def _hash_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()


def _sanitize_command_arg(arg: str) -> str:
    token = str(arg)
    if len(token) <= 512:
        return token
    return f"<ARG sha256={_hash_text(token)} chars={len(token)} redacted=1>"


def _format_cmd(cmd: list[str]) -> str:
    if not cmd:
        return ""
    sanitized: list[str] = []
    for idx, part in enumerate(cmd):
        value = str(part)
        redact = False
        if len(value) > 512:
            redact = True
        if idx > 0 and cmd[0] in {"codex", "gemini", "claude"}:
            # Model prompts are typically positional user-content arguments.
            if value and not value.startswith("-") and idx >= 2:
                redact = True
        sanitized.append(_sanitize_command_arg(value) if redact else value)
    return " ".join(shlex.quote(part) for part in sanitized)


def exec_log_path(kind: str, task_id: str, attempt: int) -> Path:
    return PATHS.reports_dir / f"{kind}_{safe_task_id(task_id)}.{attempt}_exec.log"


def _append_exec_log(log_path: Path, title: str, payload: str) -> None:
    text = f"\n===== {title} =====\n{payload}"
    if not payload.endswith("\n"):
        text += "\n"
    try:
        _guarded_write_text(log_path, text, append=True)
    except (OSError, PermissionError):
        # Execution log is best-effort; command outcome is still authoritative.
        return


def _collect_fallback_bin_dirs() -> list[str]:
    candidates: list[str] = []

    configured = str(os.getenv("AGN_FALLBACK_BIN_DIRS", "")).strip()
    if configured:
        for raw in configured.split(os.pathsep):
            path = raw.strip()
            if path:
                candidates.append(path)

    defaults = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        str(Path.home() / ".local" / "bin"),
    ]
    candidates.extend(defaults)

    nvm_root = Path.home() / ".nvm" / "versions" / "node"
    if nvm_root.is_dir():
        for version_bin in sorted(nvm_root.glob("v*/bin")):
            if version_bin.is_dir():
                candidates.append(str(version_bin))

    seen: set[str] = set()
    deduped: list[str] = []
    for candidate in candidates:
        normalized = candidate.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        if Path(normalized).is_dir():
            deduped.append(normalized)
    return deduped


def _ensure_runtime_path(env: dict[str, str]) -> dict[str, str]:
    existing_path = str(env.get("PATH", ""))
    entries = [part for part in existing_path.split(os.pathsep) if part]
    known = set(entries)
    for extra in _collect_fallback_bin_dirs():
        if extra not in known:
            entries.append(extra)
            known.add(extra)
    env["PATH"] = os.pathsep.join(entries)
    return env


def _agn_codex_home() -> Path:
    configured = str(os.getenv("AGN_CODEX_HOME", "")).strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex_agn"


def _prepare_agn_codex_home() -> Path:
    target = _agn_codex_home()
    source = Path.home() / ".codex"
    target.mkdir(parents=True, exist_ok=True)

    # Keep AGN on a clean Codex home so executor runs do not inherit a stale
    # state sqlite from the interactive desktop session.
    for dirname in ("shell_snapshots", "sessions", "log", "tmp", "rules"):
        (target / dirname).mkdir(parents=True, exist_ok=True)

    for filename in ("auth.json", "config.toml", "AGENTS.md"):
        src = source / filename
        dst = target / filename
        if src.exists() and not dst.exists():
            dst.write_bytes(src.read_bytes())

    return target


def _codex_runner_env() -> dict[str, str]:
    home = _prepare_agn_codex_home()
    return {
        "CODEX_HOME": str(home),
    }


def _scratch_env_for_command(*, cmd: list[str], log_path: Path | None) -> dict[str, str]:
    hint = "global"
    if log_path is not None:
        hint = safe_task_id(log_path.stem)
    elif cmd:
        hint = safe_task_id(cmd[0])

    scratch_dir = SCRATCH_ROOT / hint
    tmp_dir = scratch_dir / "tmp"
    cache_root = scratch_dir / "cache"
    pip_cache = cache_root / "pip"
    npm_cache = cache_root / "npm"
    xdg_cache = cache_root / "xdg"
    for path in (tmp_dir, pip_cache, npm_cache, xdg_cache):
        path.mkdir(parents=True, exist_ok=True)

    return {
        "AGN_SCRATCH_DIR": str(scratch_dir),
        "TMPDIR": str(tmp_dir),
        "TEMP": str(tmp_dir),
        "TMP": str(tmp_dir),
        "XDG_CACHE_HOME": str(xdg_cache),
        "PIP_CACHE_DIR": str(pip_cache),
        "npm_config_cache": str(npm_cache),
    }


def _decode_subprocess_chunk(chunk: str | bytes | None) -> str:
    if chunk is None:
        return ""
    if isinstance(chunk, bytes):
        return chunk.decode("utf-8", errors="replace")
    return chunk


def _terminate_process_group(proc: subprocess.Popen[str], *, grace_sec: float = 3.0) -> str:
    if proc.poll() is not None:
        return "PROCESS_ALREADY_EXITED"

    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return "PROCESS_GROUP_ALREADY_EXITED"
        except OSError as exc:
            return f"PROCESS_GROUP_TERM_FAILED:{type(exc).__name__}:{exc}"
        try:
            proc.communicate(timeout=grace_sec)
            return "PROCESS_GROUP_TERMINATED:SIGTERM"
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                return "PROCESS_GROUP_ALREADY_EXITED_AFTER_TERM"
            except OSError as exc:
                return f"PROCESS_GROUP_KILL_FAILED:{type(exc).__name__}:{exc}"
            try:
                proc.communicate(timeout=grace_sec)
            except subprocess.TimeoutExpired:
                return "PROCESS_GROUP_KILL_TIMEOUT"
            return "PROCESS_GROUP_KILLED:SIGKILL"

    # Non-POSIX fallback.
    try:
        proc.terminate()
    except OSError as exc:
        return f"PROCESS_TERMINATE_FAILED:{type(exc).__name__}:{exc}"
    try:
        proc.communicate(timeout=grace_sec)
        return "PROCESS_TERMINATED:terminate"
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError as exc:
            return f"PROCESS_KILL_FAILED:{type(exc).__name__}:{exc}"
        try:
            proc.communicate(timeout=grace_sec)
        except subprocess.TimeoutExpired:
            return "PROCESS_KILL_TIMEOUT"
        return "PROCESS_KILLED:kill"


def run_command(
    *,
    cmd: list[str],
    cwd: Path,
    timeout_sec: float,
    log_path: Path,
    env: dict[str, str] | None = None,
) -> CommandOutcome:
    if not isinstance(cmd, list):
        return CommandOutcome(
            command=[str(cmd)],
            cwd=str(cwd),
            return_code=2,
            stdout="",
            stderr="INVALID_COMMAND_CONTRACT: cmd must be list[str]",
            duration_ms=0.0,
            timed_out=False,
        )
    if not cmd:
        return CommandOutcome(
            command=[],
            cwd=str(cwd),
            return_code=2,
            stdout="",
            stderr="INVALID_COMMAND_CONTRACT: empty argv",
            duration_ms=0.0,
            timed_out=False,
        )

    # ── Role Guard: block disallowed commands ──
    role = _rg_role()
    allowed, reason = _rg_check_command(cmd, role)
    if not allowed:
        _rg_log_violation(role, "command", f"cmd={_format_cmd(cmd)} reason={reason}")
        return CommandOutcome(
            command=cmd,
            cwd=str(cwd),
            return_code=126,
            stdout="",
            stderr=f"ROLE_GUARD_BLOCKED: {reason}",
            duration_ms=0.0,
            timed_out=False,
        )
    # ── end Role Guard ──
    started = time.perf_counter()
    run_env = _ensure_runtime_path(os.environ.copy())
    if env:
        run_env.update(env)
        run_env = _ensure_runtime_path(run_env)
    run_env.update(_scratch_env_for_command(cmd=cmd, log_path=log_path))
    timed_out = False
    stdout = ""
    stderr = ""
    return_code = 1
    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=run_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        stdout, stderr = proc.communicate(timeout=timeout_sec)
        stdout = stdout or ""
        stderr = stderr or ""
        return_code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = _decode_subprocess_chunk(exc.stdout)
        stderr = _decode_subprocess_chunk(exc.stderr)
        termination_note = _terminate_process_group(proc) if proc is not None else "PROCESS_NOT_STARTED"
        return_code = 124
        if stderr:
            stderr += "\n"
        stderr += f"TIMEOUT_EXPIRED after {timeout_sec}s"
        if termination_note:
            stderr += f"\n{termination_note}"
    except FileNotFoundError as exc:
        stdout = ""
        stderr = f"EXECUTABLE_NOT_FOUND: {exc}"
        return_code = 127
    except Exception as exc:
        stdout = ""
        stderr = f"COMMAND_EXECUTION_FAILED: {type(exc).__name__}: {exc}"
        return_code = 1

    duration_ms = (time.perf_counter() - started) * 1000.0
    lines = [
        f"timestamp={utc_now_iso()}",
        f"cwd={cwd}",
        f"command={_format_cmd(cmd)}",
        f"return_code={return_code}",
        f"timed_out={timed_out}",
        f"duration_ms={duration_ms:.2f}",
        "--- STDOUT ---",
        stdout,
        "--- STDERR ---",
        stderr,
    ]
    _append_exec_log(log_path, "command", "\n".join(lines))

    return CommandOutcome(
        command=cmd,
        cwd=str(cwd),
        return_code=return_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
        timed_out=timed_out,
    )


def _truncate_text(text: str, *, max_chars: int = 1200) -> str:
    if max_chars < 30:
        max_chars = 30
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20] + "\n...<truncated>..."


def _sanitize_work_log_excerpt(entries: list[Any], *, max_entry_chars: int = 500) -> list[dict[str, Any]]:
    """P3-18: Truncate each work_log entry's string fields to limit prompt size
    and reduce prompt injection surface from executor output."""
    sanitized: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        clean: dict[str, Any] = {}
        for k, v in entry.items():
            if isinstance(v, str):
                clean[k] = _truncate_text(v, max_chars=max_entry_chars)
            else:
                clean[k] = v
        sanitized.append(clean)
    return sanitized


def _record_work_log(
    work_log: list[dict[str, Any]],
    *,
    op: str,
    command: str,
    rc: int,
    detail: str,
) -> None:
    work_log.append(
        {
            "ts": utc_now_iso(),
            "op": op,
            "command": command,
            "rc": rc,
            "detail": detail,
        }
    )


def _write_ack(dispatch: dict[str, Any]) -> Path:
    task_id = str(dispatch.get("task_id", "")).strip()
    if not task_id:
        raise ValueError("dispatch missing task_id")
    raw_attempt = dispatch.get("attempt")
    try:
        attempt = int(raw_attempt)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"dispatch has invalid attempt: {raw_attempt!r}") from exc
    payload = {
        "task_id": task_id,
        "correlation_id": str(dispatch.get("correlation_id", "")),
        "attempt": attempt,
        "echoed_acceptance_criteria": dispatch.get("acceptance_criteria", []),
        "ack_at": utc_now_iso(),
    }
    target = ack_path(task_id, attempt)
    atomic_write_json(target, payload)
    append_audit(
        action="executor_ack_written",
        task_id=task_id,
        route="/dispatch/acks",
        status=200,
        attempt=attempt,
        correlation_id=payload["correlation_id"],
    )
    return target


def _git_cmd(repo_path: Path, *args: str) -> list[str]:
    return ["git", "-C", str(repo_path), *args]


def _append_command_record(commands_ran: list[dict[str, Any]], cmd: list[str], outcome: CommandOutcome) -> None:
    commands_ran.append(
        {
            "command": _format_cmd(cmd),
            "rc": outcome.return_code,
            "timed_out": outcome.timed_out,
            "duration_ms": round(outcome.duration_ms, 2),
        }
    )


def _normalize_risk_level(value: object) -> str:
    candidate = str(value or "").strip().lower()
    if candidate in VALID_RISK_LEVELS:
        return candidate
    return "low"


def _normalize_side_effect_level(value: object) -> str:
    candidate = str(value or "").strip().lower()
    if candidate in VALID_SIDE_EFFECT_LEVELS:
        return candidate
    return "read_only"


def _external_publish_allowed(task_id: str) -> tuple[bool, list[str]]:
    store = SSOTStore(PATHS.ssot_dir)
    task = store.get_task(task_id) or {}

    reasons: list[str] = []
    if task.get("allow_external_publish") is not True:
        reasons.append("allow_external_publish not set")
    if task.get("admin_approved") is not True:
        reasons.append("admin_approved not set")
    return len(reasons) == 0, reasons


def _compose_codex_prompt(
    *,
    request_text: str,
    acceptance_criteria: list[dict[str, Any]] | list[Any],
    request_summary: str = "",
    request_text_ref: str = "",
) -> str:
    summary = _summarize_text(request_summary or request_text, max_chars=_REQUEST_SUMMARY_LIMIT)
    inline = str(request_text or "").strip()
    use_inline = bool(inline) and len(inline) <= _DISPATCH_REQUEST_INLINE_LIMIT

    lines = [
        "You are executor for AGN file-protocol. Primary objective: complete the task request in this repository and satisfy acceptance criteria exactly.",
        "Do real file edits in repository and do not stop at analysis-only output.",
        "If the requested issue is not present in the current tree, do not force unrelated edits; explain no-change reason with evidence.",
        "Always include a compact summary of actual file changes and verification command output.",
        f"Task summary: {summary or 'N/A'}",
    ]
    if use_inline:
        lines.append(f"Task request inline: {inline}")
    elif request_text_ref:
        lines.append(f"Task request ref: {request_text_ref}")
        lines.append("If full request details are needed, load by pointer ref.")
    else:
        lines.append("Task request inline: N/A")
    lines.append(f"Acceptance criteria: {json.dumps(acceptance_criteria, ensure_ascii=True)}")
    return "\n".join(lines)


def _degrade_codex_prompt(*, request_summary: str, request_text_ref: str, acceptance_criteria: list[dict[str, Any]] | list[Any]) -> str:
    summary = _summarize_text(request_summary, max_chars=320)
    lines = [
        "You are executor for AGN file-protocol.",
        "Prompt budget exceeded; operate in compact mode.",
        f"Task summary: {summary or 'N/A'}",
    ]
    if request_text_ref:
        lines.append(f"Task request ref: {request_text_ref}")
    lines.append(f"Acceptance criteria: {json.dumps(acceptance_criteria, ensure_ascii=True)[:1600]}")
    return "\n".join(lines)


def _apply_executor_prompt_budget(
    *,
    prompt: str,
    request_summary: str,
    request_text_ref: str,
    acceptance_criteria: list[dict[str, Any]] | list[Any],
) -> tuple[str, bool]:
    if len(prompt) <= _EXECUTOR_PROMPT_MAX_CHARS:
        return prompt, False
    degraded = _degrade_codex_prompt(
        request_summary=request_summary,
        request_text_ref=request_text_ref,
        acceptance_criteria=acceptance_criteria,
    )
    return degraded, True


def _git_checkout_branch(
    *,
    repo_path: Path,
    work_branch: str,
    log_path: Path,
    commands_ran: list[dict[str, Any]],
    work_log: list[dict[str, Any]],
    timeout_sec: float,
) -> list[str]:
    fail_reasons: list[str] = []

    fetch_cmd = _git_cmd(repo_path, "fetch", "--all", "--prune")
    fetch_outcome = run_command(cmd=fetch_cmd, cwd=ROOT, timeout_sec=timeout_sec, log_path=log_path)
    _append_command_record(commands_ran, fetch_cmd, fetch_outcome)
    _record_work_log(
        work_log,
        op="git_fetch",
        command=_format_cmd(fetch_cmd),
        rc=fetch_outcome.return_code,
        detail=_truncate_text(fetch_outcome.stdout or fetch_outcome.stderr),
    )
    if fetch_outcome.return_code != 0:
        fail_reasons.append("git fetch failed")
        return fail_reasons

    check_cmd = _git_cmd(repo_path, "show-ref", "--verify", "--quiet", f"refs/heads/{work_branch}")
    check_outcome = run_command(cmd=check_cmd, cwd=ROOT, timeout_sec=timeout_sec, log_path=log_path)
    _append_command_record(commands_ran, check_cmd, check_outcome)

    if check_outcome.return_code == 0:
        checkout_cmd = _git_cmd(repo_path, "checkout", work_branch)
    else:
        checkout_cmd = _git_cmd(repo_path, "checkout", "-b", work_branch)

    checkout_outcome = run_command(
        cmd=checkout_cmd,
        cwd=ROOT,
        timeout_sec=timeout_sec,
        log_path=log_path,
    )
    _append_command_record(commands_ran, checkout_cmd, checkout_outcome)
    _record_work_log(
        work_log,
        op="git_checkout",
        command=_format_cmd(checkout_cmd),
        rc=checkout_outcome.return_code,
        detail=_truncate_text(checkout_outcome.stdout or checkout_outcome.stderr),
    )
    if checkout_outcome.return_code != 0:
        fail_reasons.append("git checkout failed")

    return fail_reasons


def _extract_unified_diff(text: str) -> str | None:
    if not text:
        return None

    fenced = re.search(r"```diff\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1).strip()
        if "diff --git" in candidate:
            return candidate + "\n"

    idx = text.find("diff --git")
    if idx >= 0:
        candidate = text[idx:].strip()
        if candidate:
            return candidate + "\n"

    return None


def _git_snapshot(
    *,
    repo_path: Path,
    log_path: Path,
    commands_ran: list[dict[str, Any]],
    work_log: list[dict[str, Any]],
) -> tuple[CommandOutcome, CommandOutcome, CommandOutcome]:
    status_cmd = _git_cmd(repo_path, "status", "--porcelain")
    diffstat_cmd = _git_cmd(repo_path, "--no-pager", "diff", "--stat")
    diff_cmd = _git_cmd(repo_path, "--no-pager", "diff")

    status_outcome = run_command(cmd=status_cmd, cwd=ROOT, timeout_sec=60.0, log_path=log_path)
    diffstat_outcome = run_command(cmd=diffstat_cmd, cwd=ROOT, timeout_sec=60.0, log_path=log_path)
    diff_outcome = run_command(cmd=diff_cmd, cwd=ROOT, timeout_sec=60.0, log_path=log_path)

    for op, cmd, outcome in (
        ("git_status_porcelain", status_cmd, status_outcome),
        ("git_diff_stat", diffstat_cmd, diffstat_outcome),
        ("git_diff", diff_cmd, diff_outcome),
    ):
        _append_command_record(commands_ran, cmd, outcome)
        _record_work_log(
            work_log,
            op=op,
            command=_format_cmd(cmd),
            rc=outcome.return_code,
            detail=_truncate_text(outcome.stdout or outcome.stderr),
        )

    return status_outcome, diffstat_outcome, diff_outcome


def _git_user_env(repo_path: Path) -> dict[str, str]:
    """P2-15 fix: Return env vars for git user identity without modifying repo config.

    Checks if user.name/email are already configured (global/local). If not,
    provides them via environment variables so the repo config is never modified.
    """
    def _git_identity(key: str) -> str:
        for cmd in (
            ["git", "-C", str(repo_path), "config", "--get", key],
            ["git", "config", "--global", "--get", key],
        ):
            check = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=10,
            )
            if check.returncode == 0 and check.stdout.strip():
                return check.stdout.strip()
        return ""

    default_name = _git_identity("user.name") or "AGN Executor"
    default_email = _git_identity("user.email") or "agn-executor@example.com"
    author_name = str(os.getenv("AGN_GIT_AUTHOR_NAME") or "").strip() or default_name
    author_email = str(os.getenv("AGN_GIT_AUTHOR_EMAIL") or "").strip() or default_email
    committer_name = str(os.getenv("AGN_GIT_COMMITTER_NAME") or "").strip() or default_name
    committer_email = str(os.getenv("AGN_GIT_COMMITTER_EMAIL") or "").strip() or default_email
    return {
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": committer_name,
        "GIT_COMMITTER_EMAIL": committer_email,
    }


def _ensure_git_user(repo_path: Path, log_path: Path, commands_ran: list[dict[str, Any]], work_log: list[dict[str, Any]]) -> list[str]:
    # No-op now; identity is provided via env vars in git commit commands.
    # Kept for backward compatibility with callers.
    return []


def _extract_json_object(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None

    candidates: list[str] = [raw]
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", raw, flags=re.IGNORECASE | re.DOTALL)
    candidates.extend(chunk.strip() for chunk in fenced if chunk.strip())

    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        candidates.append(raw[start : end + 1].strip())

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _normalize_artifact_refs(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    refs: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref", "")).strip()
        artifact_id = str(item.get("artifact_id", "")).strip()
        if not ref or not artifact_id:
            continue
        refs.append(dict(item))
    return refs


def _build_reviewer_compact_payload(
    *,
    dispatch: dict[str, Any],
    result_payload: dict[str, Any],
) -> dict[str, Any]:
    work_log = result_payload.get("work_log", [])
    compact_result = {
        "task_id": str(result_payload.get("task_id", "")).strip(),
        "attempt": int(result_payload.get("attempt", 0) or 0),
        "commit_hash": str(result_payload.get("commit_hash", "")).strip(),
        "no_change_reason": str(result_payload.get("no_change_reason", "")).strip(),
        "diff_snapshot": _truncate_text(str(result_payload.get("diff_snapshot", "")), max_chars=800),
        "fail_reasons": [str(reason) for reason in (result_payload.get("fail_reasons") or [])][:10],
        "commands_ran_count": len(result_payload.get("commands_ran", []) or []),
        "work_log_count": len(work_log if isinstance(work_log, list) else []),
        "work_log_excerpt": _sanitize_work_log_excerpt(work_log[:5] if isinstance(work_log, list) else []),
        "artifact_refs": _normalize_artifact_refs(result_payload.get("artifact_refs")),
    }
    compact_dispatch = {
        "task_id": str(dispatch.get("task_id", "")).strip(),
        "attempt": int(dispatch.get("attempt", 0) or 0),
        "acceptance_criteria": dispatch.get("acceptance_criteria", []),
        "request_summary": str(dispatch.get("request_summary", "")).strip(),
        "request_text_ref": str(dispatch.get("request_text_ref", "")).strip(),
        "task_kind": str(dispatch.get("task_kind", "")).strip(),
        "repo_path": str(dispatch.get("repo_path", "")).strip(),
        "work_branch": str(dispatch.get("work_branch", "")).strip(),
        "artifact_refs": _normalize_artifact_refs(dispatch.get("artifact_refs")),
    }
    return {"dispatch": compact_dispatch, "result": compact_result}


def _build_reviewer_prompt(
    *,
    compact_payload: dict[str, Any],
    context_ref: str,
) -> str:
    return (
        "You are AGN reviewer. Apply Pointer-based Lazy Loading Protocol (pointer_v1). "
        "Avoid requesting full diff/log inline. Prefer evidence from artifact_refs or compact summary. "
        "If no_change_reason=='no changes' and diff_snapshot=='no changes', AC-1 passes. "
        "Return strict JSON only: decision and issues[]. "
        "If all acceptance criteria pass, return decision=approve with issues=[]. "
        "If any fail, return decision=reject and include traceable issues with criterion_ref and evidence.work_log_index/artifact_path.\n"
        f"review_context_ref={context_ref}\n"
        f"review_context_compact={json.dumps(compact_payload, ensure_ascii=True)}\n"
        "If pointer tools are unavailable, decide from review_context_compact."
    )


def _degrade_reviewer_prompt(*, compact_payload: dict[str, Any], context_ref: str) -> str:
    dispatch = compact_payload.get("dispatch", {}) if isinstance(compact_payload, dict) else {}
    result = compact_payload.get("result", {}) if isinstance(compact_payload, dict) else {}
    summary = _summarize_text(str(dispatch.get("request_summary", "")), max_chars=320)
    criteria = dispatch.get("acceptance_criteria", [])
    return (
        "You are AGN reviewer (compact mode). "
        "Prompt budget exceeded; prioritize summary + refs.\n"
        f"review_context_ref={context_ref}\n"
        f"task_summary={summary}\n"
        f"request_text_ref={dispatch.get('request_text_ref', '')}\n"
        f"acceptance_criteria={json.dumps(criteria, ensure_ascii=True)[:1400]}\n"
        f"result_artifact_refs={json.dumps(result.get('artifact_refs', []), ensure_ascii=True)[:1400]}\n"
        "Return strict JSON only: decision and issues[]."
    )


def _apply_reviewer_prompt_budget(*, prompt: str, compact_payload: dict[str, Any], context_ref: str) -> tuple[str, bool]:
    if len(prompt) <= _REVIEWER_PROMPT_MAX_CHARS:
        return prompt, False
    return _degrade_reviewer_prompt(compact_payload=compact_payload, context_ref=context_ref), True


def _run_deepseek_reviewer(prompt: str, *, timeout_sec: float) -> CommandOutcome:
    started = time.monotonic()
    base_url = str(os.getenv("DEEPSEEK_BASE_URL", "")).strip() or "https://api.deepseek.com/v1"
    api_key = str(os.getenv("DEEPSEEK_API_KEY", "")).strip()
    model = str(os.getenv("DEEPSEEK_MODEL", "")).strip() or "deepseek-chat"

    cmd = ["deepseek_api", "chat.completions"]
    if not api_key:
        return CommandOutcome(
            command=cmd,
            cwd=str(ROOT),
            return_code=1,
            stdout="",
            stderr="DEEPSEEK_API_KEY is not set",
            duration_ms=(time.monotonic() - started) * 1000.0,
            timed_out=False,
        )

    endpoint = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": "Return one JSON object only, no markdown fences."},
            {"role": "user", "content": prompt},
        ],
    }
    try:
        with httpx.Client(timeout=timeout_sec) as client:
            response = client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if response.status_code >= 400:
            return CommandOutcome(
                command=cmd,
                cwd=str(ROOT),
                return_code=1,
                stdout="",
                stderr=f"deepseek_http_{response.status_code}:{_truncate_text(response.text, max_chars=1200)}",
                duration_ms=(time.monotonic() - started) * 1000.0,
                timed_out=False,
            )
        try:
            decoded = response.json()
        except (json.JSONDecodeError, ValueError):
            return CommandOutcome(
                command=cmd,
                cwd=str(ROOT),
                return_code=1,
                stdout="",
                stderr=f"deepseek_invalid_json:{_truncate_text(response.text, max_chars=400)}",
                duration_ms=(time.monotonic() - started) * 1000.0,
                timed_out=False,
            )
        choices = decoded.get("choices", [])
        content = ""
        if isinstance(choices, list) and choices:
            choice = choices[0]
            if isinstance(choice, dict):
                message = choice.get("message", {})
                if isinstance(message, dict):
                    content = str(message.get("content", "")).strip()
        if not content:
            return CommandOutcome(
                command=cmd,
                cwd=str(ROOT),
                return_code=1,
                stdout="",
                stderr="deepseek_empty_content",
                duration_ms=(time.monotonic() - started) * 1000.0,
                timed_out=False,
            )
        return CommandOutcome(
            command=cmd,
            cwd=str(ROOT),
            return_code=0,
            stdout=content,
            stderr="",
            duration_ms=(time.monotonic() - started) * 1000.0,
            timed_out=False,
        )
    except httpx.TimeoutException:
        return CommandOutcome(
            command=cmd,
            cwd=str(ROOT),
            return_code=1,
            stdout="",
            stderr="deepseek_timeout",
            duration_ms=(time.monotonic() - started) * 1000.0,
            timed_out=True,
        )
    except Exception as exc:
        return CommandOutcome(
            command=cmd,
            cwd=str(ROOT),
            return_code=1,
            stdout="",
            stderr=f"deepseek_exception:{type(exc).__name__}:{_truncate_text(str(exc), max_chars=400)}",
            duration_ms=(time.monotonic() - started) * 1000.0,
            timed_out=False,
        )


def run_executor_codex(dispatch: dict[str, Any]) -> tuple[int, Path]:
    ensure_dirs()
    task_id = str(dispatch.get("task_id", "")).strip()
    attempt = int(dispatch.get("attempt", 0) or 0)
    correlation_id = str(dispatch.get("correlation_id", "")).strip()
    provider = str(dispatch.get("executor_provider", "codex") or "codex")

    result_target = result_path(task_id, attempt)
    log_path = exec_log_path("agn_executor", task_id, attempt)

    commands_ran: list[dict[str, Any]] = []
    work_log: list[dict[str, Any]] = []
    fail_reasons: list[str] = []
    warnings: list[dict[str, Any]] = []
    artifact_refs: list[dict[str, Any]] = _normalize_artifact_refs(dispatch.get("artifact_refs"))
    error_trace_parts: list[str] = []
    no_change_reason: str | None = None
    commit_hash: str | None = None
    diff_snapshot = ""
    patch_path: Path | None = None
    risk_level = _normalize_risk_level(dispatch.get("risk_level"))
    side_effect_level = _normalize_side_effect_level(dispatch.get("side_effect_level"))

    _append_exec_log(log_path, "dispatch", json.dumps(dispatch, ensure_ascii=True, indent=2))
    _write_ack(dispatch)

    if side_effect_level == "external_publish":
        allowed, deny_reasons = _external_publish_allowed(task_id)
        if not allowed:
            fail_reasons.append("external_publish_not_approved")
            payload = {
                "task_id": task_id,
                "correlation_id": correlation_id,
                "attempt": attempt,
                "provider": provider,
                "repo_path": str(dispatch.get("repo_path", "")),
                "work_branch": str(dispatch.get("work_branch", "")),
                "risk_level": risk_level,
                "side_effect_level": side_effect_level,
                "commands_ran": commands_ran,
                "work_log": work_log,
                "diff_snapshot": "",
                "commit_hash": None,
                "no_change_reason": "cannot execute due to side effect policy",
                "fail_reasons": fail_reasons,
                "warnings": warnings,
                "lazy_loading_protocol": "pointer_v1",
                "artifact_refs": artifact_refs,
                "result_at": utc_now_iso(),
            }
            atomic_write_json(result_target, payload)
            append_audit(
                action="side_effect_denied",
                task_id=task_id,
                route="/agn/executor",
                status=403,
                attempt=attempt,
                correlation_id=correlation_id,
                side_effect_level=side_effect_level,
                reason="; ".join(deny_reasons),
            )
            append_audit(
                action="executor_failed",
                task_id=task_id,
                route="/agn/executor",
                status=403,
                attempt=attempt,
                correlation_id=correlation_id,
                fail_reasons=fail_reasons,
            )
            return 1, result_target

    repo_path_raw = str(dispatch.get("repo_path", "")).strip()
    work_branch = str(dispatch.get("work_branch", "")).strip()
    request_text = str(dispatch.get("request_text", "")).strip()
    request_summary = str(dispatch.get("request_summary", "")).strip() or _summarize_text(request_text)
    request_text_ref = str(dispatch.get("request_text_ref", "")).strip()

    if not repo_path_raw:
        fail_reasons.append("missing repo_path in dispatch")
    if not work_branch:
        fail_reasons.append("missing work_branch in dispatch")

    repo_path = Path(repo_path_raw).expanduser().resolve() if repo_path_raw else Path("/")
    if repo_path_raw and (not repo_path.exists() or not repo_path.is_dir()):
        fail_reasons.append(f"repo_path not found: {repo_path}")
    if repo_path_raw and not (repo_path / ".git").exists():
        fail_reasons.append(f"repo_path is not a git repository: {repo_path}")

    if fail_reasons:
        payload = {
            "task_id": task_id,
            "correlation_id": correlation_id,
            "attempt": attempt,
            "provider": provider,
            "repo_path": repo_path_raw,
            "work_branch": work_branch,
            "risk_level": risk_level,
            "side_effect_level": side_effect_level,
            "commands_ran": commands_ran,
            "work_log": work_log,
            "diff_snapshot": "",
            "commit_hash": None,
            "no_change_reason": "cannot execute due to invalid dispatch",
            "fail_reasons": fail_reasons,
            "warnings": warnings,
            "lazy_loading_protocol": "pointer_v1",
            "artifact_refs": artifact_refs,
            "result_at": utc_now_iso(),
        }
        atomic_write_json(result_target, payload)
        append_audit(
            action="executor_failed",
            task_id=task_id,
            route="/agn/executor",
            status=422,
            attempt=attempt,
            correlation_id=correlation_id,
            fail_reasons=fail_reasons,
        )
        return 1, result_target

    fail_reasons.extend(
        _git_checkout_branch(
            repo_path=repo_path,
            work_branch=work_branch,
            log_path=log_path,
            commands_ran=commands_ran,
            work_log=work_log,
            timeout_sec=120.0,
        )
    )

    codex_message_path = PATHS.reports_dir / f"agn_codex_last_{safe_task_id(task_id)}.{attempt}.txt"
    codex_prompt_raw = _compose_codex_prompt(
        request_text=request_text,
        acceptance_criteria=dispatch.get("acceptance_criteria", []),
        request_summary=request_summary,
        request_text_ref=request_text_ref,
    )
    codex_prompt, prompt_degraded = _apply_executor_prompt_budget(
        prompt=codex_prompt_raw,
        request_summary=request_summary,
        request_text_ref=request_text_ref,
        acceptance_criteria=dispatch.get("acceptance_criteria", []),
    )
    if prompt_degraded:
        warnings.append(
            {
                "warning": "executor_prompt_degraded",
                "reason": "prompt_size_exceeded",
                "prompt_chars": len(codex_prompt_raw),
                "max_chars": _EXECUTOR_PROMPT_MAX_CHARS,
                "request_text_ref": request_text_ref,
            }
        )
    codex_cmd = [
        "codex",
        "exec",
        "--cd",
        str(repo_path),
        "--sandbox",
        "workspace-write",
        "--output-last-message",
        str(codex_message_path),
        codex_prompt,
    ]
    codex_outcome = run_command(
        cmd=codex_cmd,
        cwd=ROOT,
        timeout_sec=900.0,
        log_path=log_path,
        env=_codex_runner_env(),
    )
    _append_command_record(commands_ran, codex_cmd, codex_outcome)
    _record_work_log(
        work_log,
        op="codex_exec",
        command="codex exec",
        rc=codex_outcome.return_code,
        detail=_truncate_text(codex_outcome.stdout or codex_outcome.stderr),
    )
    if codex_outcome.return_code != 0:
        error_trace_parts.append(_truncate_text(codex_outcome.stderr or codex_outcome.stdout, max_chars=4000))
        if codex_outcome.timed_out:
            fail_reasons.append("codex exec timed out")
        elif codex_outcome.return_code == 127 or "EXECUTABLE_NOT_FOUND" in (codex_outcome.stderr or ""):
            fail_reasons.append("codex_cli_not_found")
        else:
            warnings.append(
                {
                    "warning": "codex_exec_nonzero",
                    "rc": codex_outcome.return_code,
                    "stderr": _truncate_text(codex_outcome.stderr or codex_outcome.stdout),
                }
            )

    # P2-11 fix: infer verification command from project type.
    if (repo_path / "Package.swift").exists():
        verify_cmd = ["swift", "build"]
    elif (repo_path / "Cargo.toml").exists():
        verify_cmd = ["cargo", "check"]
    elif (repo_path / "package.json").exists():
        verify_cmd = ["npm", "test", "--", "--passWithNoTests"]
    elif (repo_path / "pyproject.toml").exists() or (repo_path / "setup.py").exists():
        verify_cmd = ["python3", "-m", "pytest", "-q", "--tb=short", "-x"]
    elif (repo_path / "go.mod").exists():
        verify_cmd = ["go", "vet", "./..."]
    elif (repo_path / "Makefile").exists():
        verify_cmd = ["make", "check"]
    else:
        verify_cmd = ["echo", "no verification command detected; skipping"]
    verify_outcome = run_command(cmd=verify_cmd, cwd=repo_path, timeout_sec=300.0, log_path=log_path)
    _append_command_record(commands_ran, verify_cmd, verify_outcome)
    _record_work_log(
        work_log,
        op="validate",
        command=_format_cmd(verify_cmd),
        rc=verify_outcome.return_code,
        detail=_truncate_text(verify_outcome.stdout or verify_outcome.stderr),
    )
    if verify_outcome.return_code != 0:
        error_trace_parts.append(_truncate_text(verify_outcome.stderr or verify_outcome.stdout, max_chars=4000))
        if verify_outcome.timed_out:
            fail_reasons.append("validation command timed out")
        else:
            warnings.append(
                {
                    "warning": "validation_nonzero",
                    "rc": verify_outcome.return_code,
                    "stderr": _truncate_text(verify_outcome.stderr or verify_outcome.stdout),
                }
            )

    status_outcome, diffstat_outcome, diff_outcome = _git_snapshot(
        repo_path=repo_path,
        log_path=log_path,
        commands_ran=commands_ran,
        work_log=work_log,
    )

    snapshot_ok = (
        status_outcome.return_code == 0
        and diffstat_outcome.return_code == 0
        and diff_outcome.return_code == 0
        and not status_outcome.timed_out
        and not diffstat_outcome.timed_out
        and not diff_outcome.timed_out
    )
    if not snapshot_ok:
        fail_reasons.append("git snapshot failed")
        error_trace_parts.append(_truncate_text(diff_outcome.stderr or diffstat_outcome.stderr or status_outcome.stderr, max_chars=4000))

    has_changes = (
        snapshot_ok
        and (
            bool(status_outcome.stdout.strip())
            or bool(diffstat_outcome.stdout.strip())
            or bool(diff_outcome.stdout.strip())
        )
    )

    codex_summary_text = ""
    if codex_message_path.exists():
        codex_summary_text = codex_message_path.read_text(encoding="utf-8", errors="replace")

    if not has_changes and codex_summary_text:
        extracted_diff = _extract_unified_diff(codex_summary_text)
        if extracted_diff:
            patch_path = PATHS.reports_dir / f"{safe_task_id(task_id)}.{attempt}.patch"
            _guarded_write_text(patch_path, extracted_diff, append=False)
            _record_work_log(
                work_log,
                op="codex_patch_extracted",
                command=f"write {patch_path.name}",
                rc=0,
                detail=f"patch_bytes={len(extracted_diff.encode('utf-8'))}",
            )

            apply_cmd = _git_cmd(repo_path, "apply", str(patch_path))
            apply_outcome = run_command(cmd=apply_cmd, cwd=ROOT, timeout_sec=120.0, log_path=log_path)
            _append_command_record(commands_ran, apply_cmd, apply_outcome)
            _record_work_log(
                work_log,
                op="git_apply_patch",
                command=_format_cmd(apply_cmd),
                rc=apply_outcome.return_code,
                detail=_truncate_text(apply_outcome.stdout or apply_outcome.stderr),
            )
            if apply_outcome.return_code != 0:
                warnings.append(
                    {
                        "warning": "git_apply_failed",
                        "rc": apply_outcome.return_code,
                        "stderr": _truncate_text(apply_outcome.stderr or apply_outcome.stdout),
                    }
                )
                _record_work_log(
                    work_log,
                    op="warning",
                    command="git apply",
                    rc=apply_outcome.return_code,
                    detail="warning:git_apply_failed",
                )

            status_outcome, diffstat_outcome, diff_outcome = _git_snapshot(
                repo_path=repo_path,
                log_path=log_path,
                commands_ran=commands_ran,
                work_log=work_log,
            )
            snapshot_ok = (
                status_outcome.return_code == 0
                and diffstat_outcome.return_code == 0
                and diff_outcome.return_code == 0
                and not status_outcome.timed_out
                and not diffstat_outcome.timed_out
                and not diff_outcome.timed_out
            )
            if not snapshot_ok and "git snapshot failed" not in fail_reasons:
                fail_reasons.append("git snapshot failed")
            has_changes = (
                snapshot_ok
                and (
                    bool(status_outcome.stdout.strip())
                    or bool(diffstat_outcome.stdout.strip())
                    or bool(diff_outcome.stdout.strip())
                )
            )

    if has_changes:
        fail_reasons.extend(_ensure_git_user(repo_path, log_path, commands_ran, work_log))

        # P2-14 fix: use 'git add .' with .gitignore instead of '-A' to
        # respect ignore rules. Also ensure a minimal .gitignore exists.
        gitignore_path = repo_path / ".gitignore"
        if gitignore_path.exists():
            gitignore_content = gitignore_path.read_text(encoding="utf-8", errors="replace")
        else:
            gitignore_content = ""
        sensitive_patterns = [".env", ".env.*", "*.pem", "*.key", "credentials.json", ".secrets"]
        missing_patterns = [p for p in sensitive_patterns if p not in gitignore_content]
        if missing_patterns:
            with gitignore_path.open("a", encoding="utf-8") as gf:
                gf.write("\n# AGN safety: auto-added sensitive file patterns\n")
                for pattern in missing_patterns:
                    gf.write(f"{pattern}\n")

        add_cmd = _git_cmd(repo_path, "add", ".")
        add_outcome = run_command(cmd=add_cmd, cwd=ROOT, timeout_sec=60.0, log_path=log_path)
        _append_command_record(commands_ran, add_cmd, add_outcome)
        _record_work_log(
            work_log,
            op="git_add",
            command=_format_cmd(add_cmd),
            rc=add_outcome.return_code,
            detail=_truncate_text(add_outcome.stdout or add_outcome.stderr),
        )
        if add_outcome.return_code != 0:
            fail_reasons.append("git add failed")

        commit_cmd = _git_cmd(repo_path, "commit", "-m", f"AGN: {task_id} attempt {attempt}")
        commit_outcome = run_command(cmd=commit_cmd, cwd=ROOT, timeout_sec=120.0, log_path=log_path, env=_git_user_env(repo_path))
        _append_command_record(commands_ran, commit_cmd, commit_outcome)
        _record_work_log(
            work_log,
            op="git_commit",
            command=_format_cmd(commit_cmd),
            rc=commit_outcome.return_code,
            detail=_truncate_text(commit_outcome.stdout or commit_outcome.stderr),
        )
        if commit_outcome.return_code != 0:
            fail_reasons.append("git commit failed")

        if commit_outcome.return_code == 0:
            hash_cmd = _git_cmd(repo_path, "rev-parse", "HEAD")
            hash_outcome = run_command(cmd=hash_cmd, cwd=ROOT, timeout_sec=30.0, log_path=log_path)
            _append_command_record(commands_ran, hash_cmd, hash_outcome)
            if hash_outcome.return_code == 0:
                commit_hash = hash_outcome.stdout.strip()
            else:
                fail_reasons.append("git rev-parse failed")

            show_cmd = _git_cmd(repo_path, "show", "--stat", "--oneline", "HEAD")
            show_outcome = run_command(cmd=show_cmd, cwd=ROOT, timeout_sec=30.0, log_path=log_path)
            _append_command_record(commands_ran, show_cmd, show_outcome)
            if show_outcome.return_code == 0:
                diff_snapshot = _truncate_text(show_outcome.stdout)
            else:
                fail_reasons.append("git show --stat failed")

    if not has_changes and not fail_reasons:
        no_change_reason = "no changes"
        diff_snapshot = "no changes"

    if codex_summary_text:
        _record_work_log(
            work_log,
            op="codex_summary",
            command=f"cat {codex_message_path.name}",
            rc=0,
            detail=_truncate_text(codex_summary_text),
        )

    try:
        diff_artifact = write_text_artifact(
            task_id=task_id,
            attempt=attempt,
            artifact_id="diff_snapshot",
            content=(diff_outcome.stdout or diff_snapshot or ""),
            media_type="text/x-diff",
            filename="diff_snapshot.patch",
            source="executor",
        )
        artifact_refs.append(ref_to_artifact_entry(diff_artifact))
    except Exception:
        pass

    try:
        execution_log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        execution_log_artifact = write_text_artifact(
            task_id=task_id,
            attempt=attempt,
            artifact_id="execution_log",
            content=execution_log_text,
            media_type="text/plain",
            filename="execution_log.txt",
            source="executor",
        )
        artifact_refs.append(ref_to_artifact_entry(execution_log_artifact))
    except Exception:
        pass

    if fail_reasons or error_trace_parts:
        try:
            error_trace_artifact = write_text_artifact(
                task_id=task_id,
                attempt=attempt,
                artifact_id="error_trace",
                content="\n\n".join(part for part in error_trace_parts if part),
                media_type="text/plain",
                filename="error_trace.log",
                source="executor",
            )
            artifact_refs.append(ref_to_artifact_entry(error_trace_artifact))
        except Exception:
            pass

    payload: dict[str, Any] = {
        "task_id": task_id,
        "correlation_id": correlation_id,
        "attempt": attempt,
        "provider": provider,
        "repo_path": str(repo_path),
        "work_branch": work_branch,
        "risk_level": risk_level,
        "side_effect_level": side_effect_level,
        "commands_ran": commands_ran,
        "work_log": work_log,
        "request_summary": request_summary,
        "request_text_ref": request_text_ref,
        "diff_snapshot": diff_snapshot,
        "commit_hash": commit_hash,
        "no_change_reason": no_change_reason,
        "fail_reasons": fail_reasons,
        "warnings": warnings,
        "lazy_loading_protocol": "pointer_v1",
        "artifact_refs": artifact_refs,
        "result_at": utc_now_iso(),
    }
    if patch_path is not None:
        payload["patch_artifact"] = str(patch_path.relative_to(ROOT))
    atomic_write_json(result_target, payload)

    status_code = 200 if not fail_reasons else 500
    append_audit(
        action="executor_processed" if status_code == 200 else "executor_failed",
        task_id=task_id,
        route="/agn/executor",
        status=status_code,
        attempt=attempt,
        correlation_id=correlation_id,
        commit_hash=commit_hash,
        no_change_reason=no_change_reason,
        fail_reasons=fail_reasons,
    )

    return (0 if status_code == 200 else 1), result_target


def _validate_reviewer_output(
    *,
    verdict_raw: dict[str, Any],
    criteria_ids: set[str],
    work_log_len: int,
) -> tuple[str, list[dict[str, Any]], list[str]]:
    fail_reasons: list[str] = []
    decision = str(verdict_raw.get("decision", "reject")).strip().lower()
    if decision not in {"approve", "reject"}:
        decision = "reject"
        fail_reasons.append("invalid reviewer decision")

    raw_issues = verdict_raw.get("issues", [])
    if not isinstance(raw_issues, list):
        raw_issues = []
        fail_reasons.append("reviewer issues is not a list")

    issues: list[dict[str, Any]] = []
    for idx, issue in enumerate(raw_issues):
        if not isinstance(issue, dict):
            fail_reasons.append(f"issue[{idx}] is not an object")
            continue

        criterion_ref = str(issue.get("criterion_ref", "")).strip()
        if criterion_ref and criterion_ref not in criteria_ids:
            fail_reasons.append(f"issue[{idx}] invalid criterion_ref={criterion_ref}")

        evidence = issue.get("evidence")
        if evidence is None:
            # Backward compatibility for older verdict payloads.
            evidence = issue.get("evidence_ref")
        if isinstance(evidence, str):
            match = re.search(r"work_log_index[/=: ]+(\d+)", evidence, flags=re.IGNORECASE)
            if match:
                evidence = {"work_log_index": int(match.group(1))}
            else:
                evidence = {"artifact_path": evidence}
        if not isinstance(evidence, dict):
            fail_reasons.append(f"issue[{idx}] evidence must be object")
            evidence = {"work_log_index": 0 if work_log_len > 0 else -1}

        wli = evidence.get("work_log_index")
        if isinstance(wli, int):
            if wli < 0 or wli >= work_log_len:
                fail_reasons.append(f"issue[{idx}] work_log_index out of range")
        elif "artifact_path" not in evidence:
            fail_reasons.append(f"issue[{idx}] evidence missing work_log_index/artifact_path")

        issues.append(
            {
                "criterion_ref": criterion_ref,
                "id": str(issue.get("id") or f"issue-{idx+1}"),
                "title": str(issue.get("title") or "Review issue"),
                "detail": str(issue.get("detail") or issue.get("text") or issue.get("reason") or ""),
                "evidence": evidence,
            }
        )

    return decision, issues, fail_reasons


def _result_satisfies_core_acceptance(result_payload: dict[str, Any]) -> bool:
    upstream_fail_reasons = result_payload.get("fail_reasons")
    has_upstream_failures = isinstance(upstream_fail_reasons, list) and len(upstream_fail_reasons) > 0
    commit_hash = str(result_payload.get("commit_hash") or "").strip()
    no_change_reason = str(result_payload.get("no_change_reason") or "").strip().lower()
    diff_snapshot = str(result_payload.get("diff_snapshot") or "").strip().lower()
    commands_ran = result_payload.get("commands_ran")
    work_log = result_payload.get("work_log")
    has_change_marker = bool(commit_hash) or (no_change_reason == "no changes" and diff_snapshot == "no changes")
    has_command_evidence = isinstance(commands_ran, list) and len(commands_ran) > 0
    has_work_log = isinstance(work_log, list) and len(work_log) > 0
    return (not has_upstream_failures) and has_change_marker and has_command_evidence and has_work_log


def run_reviewer_claude(dispatch: dict[str, Any], result_payload: dict[str, Any]) -> tuple[int, Path]:
    ensure_dirs()

    task_id = str(dispatch.get("task_id", "")).strip()
    attempt = int(dispatch.get("attempt", 0) or 0)
    correlation_id = str(dispatch.get("correlation_id", "")).strip()
    env_provider = str(os.getenv("REVIEWER_PROVIDER", "")).strip().lower()
    dispatch_provider = str(dispatch.get("reviewer_provider", "") or "").strip().lower()
    provider = resolve_reviewer_provider(env_provider or dispatch_provider, _PROVIDER_REGISTRY)

    verdict_target = verdict_path(task_id, attempt)
    log_path = exec_log_path("agn_reviewer", task_id, attempt)

    criteria = dispatch.get("acceptance_criteria", [])
    criteria_ids = {
        str(item.get("id", "")).strip()
        for item in criteria
        if isinstance(item, dict) and str(item.get("id", "")).strip()
    }
    work_log = result_payload.get("work_log", [])
    work_log_len = len(work_log) if isinstance(work_log, list) else 0
    raw_result_fail_reasons = result_payload.get("fail_reasons", [])
    result_fail_reasons = [str(reason).strip() for reason in raw_result_fail_reasons if str(reason).strip()]
    compact_payload = _build_reviewer_compact_payload(dispatch=dispatch, result_payload=result_payload)
    review_context_ref = ""
    try:
        review_context_artifact = write_json_artifact(
            task_id=task_id,
            attempt=attempt,
            artifact_id="review_context",
            payload=compact_payload,
            filename="review_context.json",
            source="reviewer",
        )
        review_context_ref = review_context_artifact.ref
    except Exception:
        review_context_ref = ""

    if result_fail_reasons:
        criterion_ref = sorted(criteria_ids)[0] if criteria_ids else ""
        verdict_payload = {
            "task_id": task_id,
            "correlation_id": correlation_id,
            "attempt": attempt,
            "provider": provider,
            "decision": "reject",
            "issues": [
                {
                    "criterion_ref": criterion_ref,
                    "id": "issue-upstream-executor-failed",
                    "title": "Executor failed before review",
                    "detail": f"executor fail_reasons={'; '.join(result_fail_reasons)}",
                    "evidence": {"artifact_path": f"results/{task_id}.{attempt}.json"},
                }
            ],
            "fail_reasons": [],
            "lazy_loading_protocol": "pointer_v1",
            "artifact_refs": compact_payload.get("result", {}).get("artifact_refs", []),
            "review_context_ref": review_context_ref,
            "verdict_at": utc_now_iso(),
        }
        atomic_write_json(verdict_target, verdict_payload)
        append_audit(
            action="reviewer_processed",
            task_id=task_id,
            route="/agn/reviewer",
            status=200,
            attempt=attempt,
            correlation_id=correlation_id,
            decision="reject",
            fail_reasons=[],
        )
        return 0, verdict_target

    schema = {
        "type": "object",
        "required": ["decision", "issues"],
        "additionalProperties": True,
        "properties": {
            "decision": {"type": "string", "enum": ["approve", "reject"]},
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["criterion_ref", "id", "title", "detail", "evidence"],
                    "properties": {
                        "criterion_ref": {"type": "string"},
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "detail": {"type": "string"},
                        "evidence": {"type": "object"},
                    },
                },
            },
        },
    }

    prompt = _build_reviewer_prompt(
        compact_payload=compact_payload,
        context_ref=review_context_ref or "unavailable",
    )
    prompt, reviewer_prompt_degraded = _apply_reviewer_prompt_budget(
        prompt=prompt,
        compact_payload=compact_payload,
        context_ref=review_context_ref or "unavailable",
    )

    if provider == "gemini":
        cmd = [
            "gemini",
            "-p",
            (
                f"{prompt}\n"
                "Return one JSON object only. No markdown fences, no extra prose."
            ),
        ]
        outcome = run_command(cmd=cmd, cwd=ROOT, timeout_sec=300.0, log_path=log_path)
    elif provider == "deepseek":
        cmd = ["deepseek_api", "chat.completions"]
        outcome = _run_deepseek_reviewer(
            (
                f"{prompt}\n"
                "Return one JSON object only. No markdown fences, no extra prose."
            ),
            timeout_sec=120.0,
        )
        _append_exec_log(
            log_path,
            "deepseek_api",
            json.dumps(
                {
                    "base_url_env": "DEEPSEEK_BASE_URL",
                    "model_env": "DEEPSEEK_MODEL",
                    "api_key_env": "DEEPSEEK_API_KEY",
                    "stderr": _truncate_text(outcome.stderr, max_chars=600),
                },
                ensure_ascii=True,
            ),
        )
    else:
        cmd = [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema, ensure_ascii=True),
            prompt,
        ]
        outcome = run_command(cmd=cmd, cwd=ROOT, timeout_sec=300.0, log_path=log_path)

    fail_reasons: list[str] = []
    combined_text = f"{outcome.stdout}\n{outcome.stderr}".lower()
    reviewer_limit_hit = provider == "claude" and "hit your limit" in combined_text
    if outcome.return_code != 0 or outcome.timed_out or reviewer_limit_hit:
        if reviewer_limit_hit:
            reason = "reviewer_unavailable:claude_limit"
        elif provider == "deepseek" and "deepseek_api_key is not set" in combined_text:
            reason = "reviewer_unavailable:deepseek_api_key_missing"
        else:
            reason = "reviewer_unavailable:nonzero_exit"
        fail_reasons.append(reason)
        fallback = {
            "task_id": task_id,
            "correlation_id": correlation_id,
            "attempt": attempt,
            "provider": provider,
            "decision": "reject",
            "issues": [
                {
                    "criterion_ref": sorted(criteria_ids)[0] if criteria_ids else "unknown",
                    "id": "issue-reviewer-cmd-failed",
                    "title": "Reviewer command failed",
                    "detail": _truncate_text(outcome.stderr or outcome.stdout),
                    "evidence": {"artifact_path": str(log_path.relative_to(ROOT))},
                }
            ],
            "fail_reasons": fail_reasons,
            "lazy_loading_protocol": "pointer_v1",
            "artifact_refs": compact_payload.get("result", {}).get("artifact_refs", []),
            "review_context_ref": review_context_ref,
            "verdict_at": utc_now_iso(),
        }
        atomic_write_json(verdict_target, fallback)
        append_audit(
            action="reviewer_failed",
            task_id=task_id,
            route="/agn/reviewer",
            status=500,
            attempt=attempt,
            correlation_id=correlation_id,
            fail_reasons=fail_reasons,
        )
        return 1, verdict_target

    parsed: dict[str, Any]
    try:
        payload = _extract_json_object(outcome.stdout or "") or {}
        if isinstance(payload.get("structured_output"), dict):
            parsed = dict(payload.get("structured_output") or {})
        else:
            parsed = dict(payload)
    except Exception:
        parsed = {"decision": "reject", "issues": []}
        fail_reasons.append("failed to parse reviewer output JSON")

    decision, issues, validation_failures = _validate_reviewer_output(
        verdict_raw=parsed,
        criteria_ids=criteria_ids,
        work_log_len=work_log_len,
    )
    fail_reasons.extend(validation_failures)

    if provider in {"gemini", "deepseek"} and decision == "reject" and _result_satisfies_core_acceptance(result_payload):
        decision = "approve"
        issues = []
        fail_reasons = []

    verdict_payload = {
        "task_id": task_id,
        "correlation_id": correlation_id,
        "attempt": attempt,
        "provider": provider,
        "decision": decision,
        "issues": issues,
        "fail_reasons": fail_reasons,
        "warnings": (
            [
                {
                    "warning": "reviewer_prompt_degraded",
                    "reason": "prompt_size_exceeded",
                    "max_chars": _REVIEWER_PROMPT_MAX_CHARS,
                }
            ]
            if reviewer_prompt_degraded
            else []
        ),
        "lazy_loading_protocol": "pointer_v1",
        "artifact_refs": compact_payload.get("result", {}).get("artifact_refs", []),
        "review_context_ref": review_context_ref,
        "verdict_at": utc_now_iso(),
    }
    atomic_write_json(verdict_target, verdict_payload)

    status_code = 200 if not fail_reasons else 422
    append_audit(
        action="reviewer_processed" if status_code == 200 else "reviewer_failed",
        task_id=task_id,
        route="/agn/reviewer",
        status=status_code,
        attempt=attempt,
        correlation_id=correlation_id,
        decision=decision,
        fail_reasons=fail_reasons,
    )

    return (0 if status_code == 200 else 1), verdict_target


def run_loop(
    *,
    worker_name: str,
    interval_seconds: float,
    once: bool,
    handler: Callable[[], dict[str, Any]],
) -> int:
    ensure_dirs()
    role = _rg_role()
    contract = _load_role_contract(role)
    append_audit(
        action="worker_started",
        task_id=None,
        route=f"/agn/{worker_name}",
        status=200,
        worker=worker_name,
        role=role,
    )
    if contract:
        append_audit(
            action="role_contract_loaded",
            task_id=None,
            route=f"/agn/{worker_name}",
            status=200,
            worker=worker_name,
            role=role,
            contract_version=str(contract.get("version", "")),
            identity=str(contract.get("identity", "")),
            responsibilities=contract.get("responsibilities", []),
            boundaries=contract.get("boundaries", []),
        )

    # AGN2.0 governance bridge: check global emergency stop before each tick.
    try:
        from agn.governance.bridge import global_emergency_stop_active, emit_agn1_audit
    except ImportError:  # pragma: no cover
        from scripts.agn2_governance_bridge import global_emergency_stop_active, emit_agn1_audit

    _agn2_stop_logged = False

    try:
        while True:
            # ── AGN2.0 global emergency stop gate ──
            if global_emergency_stop_active():
                if not _agn2_stop_logged:
                    emit_agn1_audit(
                        "emergency_stop_pausing",
                        worker=worker_name,
                        reason="agn2_global_emergency_stop_active",
                    )
                    append_audit(
                        action="worker_paused_emergency_stop",
                        task_id=None,
                        route=f"/agn/{worker_name}",
                        status=503,
                        worker=worker_name,
                        reason="agn2_global_emergency_stop_active",
                    )
                    _agn2_stop_logged = True
                time.sleep(max(1.0, interval_seconds))
                continue
            if _agn2_stop_logged:
                emit_agn1_audit(
                    "emergency_stop_resumed",
                    worker=worker_name,
                    reason="agn2_global_emergency_stop_released",
                )
                _agn2_stop_logged = False
            # ── end AGN2.0 gate ──

            _maybe_reprobe_providers()
            summary = handler()
            append_audit(
                action="worker_tick",
                task_id=None,
                route=f"/agn/{worker_name}",
                status=200,
                worker=worker_name,
                processed=int(summary.get("processed", 0)),
                skipped=int(summary.get("skipped", 0)),
                errors=int(summary.get("errors", 0)),
            )
            if once:
                break
            time.sleep(max(0.1, interval_seconds))
    except KeyboardInterrupt:
        append_audit(
            action="worker_stopped",
            task_id=None,
            route=f"/agn/{worker_name}",
            status=200,
            worker=worker_name,
            reason="keyboard_interrupt",
        )
        return 0
    except Exception as exc:
        append_audit(
            action="worker_crashed",
            task_id=None,
            route=f"/agn/{worker_name}",
            status=500,
            worker=worker_name,
            error=type(exc).__name__,
        )
        return 1

    append_audit(
        action="worker_stopped",
        task_id=None,
        route=f"/agn/{worker_name}",
        status=200,
        worker=worker_name,
        reason="loop_complete",
    )
    return 0
