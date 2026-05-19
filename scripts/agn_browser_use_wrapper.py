#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from emergency_stop import load_system_mode
except ImportError:  # pragma: no cover
    from scripts.emergency_stop import load_system_mode

ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = ROOT / "reports"
WRAPPER = "browser-use"
DISABLE_ENV = "AGN_BROWSER_USE_WRAPPER_DISABLED"
SESSION_PREFIX = "agn-browser-use-wrapper-"
DEFAULT_URL = "https://example.com"
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_SETTLE_SECONDS = 2.0
DEFAULT_MAX_ACTIVE_SESSIONS = 1
SCREENSHOT_SUFFIX = ".png"
REPORT_SUFFIX = ".json"
STATE_SUFFIX = ".state.json"
BROWSER_USE_BIN_ENV = "AGN_BROWSER_USE_BIN"
BROWSER_HOME = Path.home() / ".browser-use"
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")

INPUT_CONTRACT = {
    "command": "run",
    "required": ["url"],
    "optional": [
        "session",
        "timeout_seconds",
        "artifact_stem",
        "headed",
        "profile",
        "connect",
        "cdp_url",
        "settle_seconds",
        "keep_session",
        "max_active_sessions",
    ],
    "constraints": {
        "url": "must be an absolute http or https URL",
        "session": f"must match {SESSION_ID_RE.pattern} and start with {SESSION_PREFIX!r} when supplied",
        "timeout_seconds": "integer between 5 and 300",
        "artifact_stem": "ASCII-safe file stem; wrapper defaults to a timestamped value",
        "headed": "boolean; false keeps browser-use in its default background posture",
        "profile": "optional Chrome profile name; explicit profile attach may surface a real browser window",
        "connect": "boolean; auto-discovers a running Chrome instance only when explicitly requested",
        "cdp_url": "optional ws/http CDP endpoint; cannot be combined with profile/connect",
        "settle_seconds": "number between 0 and 15 to allow the page to stabilize before capture",
        "keep_session": "boolean; false closes and cleans up the wrapper-owned session after capture",
        "max_active_sessions": "integer between 1 and 5; wrapper refuses to pile up AGN-managed sessions beyond this budget",
    },
}
OUTPUT_CONTRACT = {
    "fields": [
        "ok",
        "wrapper",
        "action",
        "session",
        "artifacts",
        "authority_boundary",
        "kill_switch",
        "rollback",
        "steps",
    ],
    "artifacts": {
        "report": "JSON artifact with command logs and boundaries",
        "state": "JSON browser state snapshot after navigation",
        "screenshot": "PNG screenshot produced by browser-use",
    },
}
AUTHORITY_BOUNDARY = {
    "scope": "execution-only browser automation helper",
    "allowed_commands": ["open", "state", "screenshot", "close"],
    "prohibited_capabilities": [
        "no control-plane writes",
        "no dispatcher routing changes",
        "no governance mutations",
        "no implicit reuse of operator sessions without explicit wrapper support",
        "no silent profile/cdp/cloud escalation through the default wrapper path",
    ],
}


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _kill_switch_state() -> dict[str, Any]:
    return {
        "env_var": DISABLE_ENV,
        "active": os.getenv(DISABLE_ENV, "").strip().lower() in {"1", "true", "yes", "on"},
    }


def _system_mode() -> dict[str, Any]:
    return load_system_mode()


def _validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("url must be an absolute http or https URL")
    return url


def _validate_session(session: str | None) -> str:
    if not session:
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{SESSION_PREFIX}{stamp}"
    if not SESSION_ID_RE.match(session):
        raise ValueError("session does not match the allowed pattern")
    if not session.startswith(SESSION_PREFIX):
        raise ValueError(f"session must start with {SESSION_PREFIX!r}")
    return session


def _validate_timeout(timeout_seconds: int) -> int:
    if timeout_seconds < 5 or timeout_seconds > 300:
        raise ValueError("timeout_seconds must be between 5 and 300")
    return timeout_seconds


def _validate_settle_seconds(settle_seconds: float) -> float:
    if settle_seconds < 0 or settle_seconds > 15:
        raise ValueError("settle_seconds must be between 0 and 15")
    return round(float(settle_seconds), 3)


def _validate_attachment(profile: str, connect: bool, cdp_url: str) -> dict[str, Any]:
    profile = str(profile or "").strip()
    cdp_url = str(cdp_url or "").strip()
    attach_modes = sum(bool(value) for value in [profile, connect, cdp_url])
    if attach_modes > 1:
        raise ValueError("profile, connect, and cdp_url are mutually exclusive")
    if cdp_url and not (cdp_url.startswith("http://") or cdp_url.startswith("https://") or cdp_url.startswith("ws://") or cdp_url.startswith("wss://")):
        raise ValueError("cdp_url must start with http://, https://, ws://, or wss://")
    return {
        "profile": profile,
        "connect": bool(connect),
        "cdp_url": cdp_url,
    }


def _validate_max_active_sessions(max_active_sessions: int) -> int:
    if max_active_sessions < 1 or max_active_sessions > 5:
        raise ValueError("max_active_sessions must be between 1 and 5")
    return max_active_sessions


def _artifact_stem(explicit: str | None) -> str:
    if explicit:
        return re.sub(r"[^A-Za-z0-9._-]", "-", explicit).strip("-._") or "browser-use-wrapper"
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"agn_browser_use_wrapper_{stamp}"


def _artifacts(stem: str) -> dict[str, str]:
    return {
        "report": str(REPORTS_DIR / f"{stem}{REPORT_SUFFIX}"),
        "state": str(REPORTS_DIR / f"{stem}{STATE_SUFFIX}"),
        "screenshot": str(REPORTS_DIR / f"{stem}{SCREENSHOT_SUFFIX}"),
    }


def _write_json(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def _session_runtime_paths(session: str) -> list[str]:
    return [
        str(BROWSER_HOME / f"{session}.sock"),
        str(BROWSER_HOME / f"{session}.pid"),
    ]


def _browser_use_candidates() -> list[Path]:
    home = Path.home()
    return [
        home / ".browser-use-env" / "bin" / "browser-use",
        home / ".agn_external_wrappers_venv" / "bin" / "browser-use",
    ]


def _browser_use_bin() -> Path:
    override = str(os.getenv(BROWSER_USE_BIN_ENV, "")).strip()
    if override:
        return Path(override).expanduser().resolve()
    for candidate in _browser_use_candidates():
        if candidate.exists():
            return candidate
    return _browser_use_candidates()[0]


def _browser_use_context_args(headed: bool, profile: str, connect: bool, cdp_url: str) -> list[str]:
    context_args: list[str] = []
    if headed:
        context_args.append("--headed")
    if profile:
        context_args.extend(["--profile", profile])
    elif connect:
        context_args.append("--connect")
    elif cdp_url:
        context_args.extend(["--cdp-url", cdp_url])
    return context_args


def _run_browser_use(session: str, timeout_seconds: int, context_args: list[str], *subcommand: str) -> dict[str, Any]:
    browser_use_bin = _browser_use_bin()
    if not browser_use_bin.exists():
        searched = [str(path) for path in _browser_use_candidates()]
        return {
            "command": [str(browser_use_bin), *context_args, "--json", "--session", session, *subcommand],
            "returncode": 127,
            "duration_seconds": 0,
            "stdout": "",
            "stderr": f"browser-use binary not found; searched {searched}",
            "parsed": None,
        }
    command = [str(browser_use_bin), *context_args, "--json", "--session", session, *subcommand]
    started = time.time()
    try:
        proc = subprocess.run(
            command,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "returncode": 124,
            "duration_seconds": round(time.time() - started, 3),
            "stdout": ((exc.stdout or "") if isinstance(exc.stdout, str) else ""),
            "stderr": f"timed out after {timeout_seconds} seconds",
            "parsed": None,
            "timed_out": True,
        }
    stdout = (proc.stdout or "").strip()
    parsed: dict[str, Any] | None = None
    if stdout:
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            parsed = None
    return {
        "command": command,
        "returncode": proc.returncode,
        "duration_seconds": round(time.time() - started, 3),
        "stdout": stdout,
        "stderr": (proc.stderr or "").strip(),
        "parsed": parsed,
    }


def _list_active_sessions(timeout_seconds: int) -> dict[str, Any]:
    browser_use_bin = _browser_use_bin()
    command = [str(browser_use_bin), "--json", "sessions"]
    started = time.time()
    if not browser_use_bin.exists():
        searched = [str(path) for path in _browser_use_candidates()]
        return {
            "command": command,
            "returncode": 127,
            "duration_seconds": 0,
            "stdout": "",
            "stderr": f"browser-use binary not found; searched {searched}",
            "parsed": None,
        }
    try:
        proc = subprocess.run(
            command,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "command": command,
            "returncode": 124,
            "duration_seconds": round(time.time() - started, 3),
            "stdout": "",
            "stderr": f"timed out after {timeout_seconds} seconds",
            "parsed": None,
        }
    stdout = (proc.stdout or "").strip()
    parsed: dict[str, Any] | None = None
    if stdout:
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            parsed = None
    return {
        "command": command,
        "returncode": proc.returncode,
        "duration_seconds": round(time.time() - started, 3),
        "stdout": stdout,
        "stderr": (proc.stderr or "").strip(),
        "parsed": parsed,
    }


def _close_session(session: str, timeout_seconds: int, context_args: list[str]) -> dict[str, Any]:
    try:
        return _run_browser_use(session, timeout_seconds, context_args, "close")
    except Exception as exc:  # pragma: no cover
        return {
            "command": [str(_browser_use_bin()), *context_args, "--json", "--session", session, "close"],
            "returncode": 1,
            "duration_seconds": 0,
            "stdout": "",
            "stderr": str(exc),
            "parsed": None,
        }


def _close_step_ok(step: dict[str, Any]) -> bool:
    if step.get("returncode") == 0:
        return True
    combined = f"{step.get('stdout', '')}\n{step.get('stderr', '')}".lower()
    return "not found" in combined or "no such file" in combined or "refused" in combined


def _terminate_session_process(session: str) -> dict[str, Any]:
    pid_path = BROWSER_HOME / f"{session}.pid"
    if not pid_path.exists():
        return {
            "pid_path": str(pid_path),
            "attempted": False,
            "terminated": False,
            "reason": "pid_file_missing",
        }
    raw = pid_path.read_text(encoding="utf-8").strip()
    if not raw.isdigit():
        return {
            "pid_path": str(pid_path),
            "attempted": False,
            "terminated": False,
            "reason": "invalid_pid_contents",
            "raw": raw,
        }
    pid = int(raw)
    try:
        os.kill(pid, signal.SIGTERM)
        return {
            "pid_path": str(pid_path),
            "attempted": True,
            "terminated": True,
            "pid": pid,
        }
    except ProcessLookupError:
        return {
            "pid_path": str(pid_path),
            "attempted": True,
            "terminated": False,
            "pid": pid,
            "reason": "process_missing",
        }
    except PermissionError as exc:
        return {
            "pid_path": str(pid_path),
            "attempted": True,
            "terminated": False,
            "pid": pid,
            "reason": str(exc),
        }


def _cleanup_session_runtime_paths(session: str) -> list[str]:
    removed_paths: list[str] = []
    for candidate in _session_runtime_paths(session):
        path = Path(candidate)
        if path.exists():
            path.unlink()
            removed_paths.append(str(path))
    return removed_paths


def _prune_stale_runtime_paths(timeout_seconds: int, session_prefix: str) -> dict[str, Any]:
    sessions_step = _list_active_sessions(timeout_seconds)
    active_sessions: set[str] = set()
    parsed = sessions_step.get("parsed") or {}
    if isinstance(parsed, dict):
        active_sessions = {
            str(entry.get("name"))
            for entry in parsed.get("sessions", [])
            if isinstance(entry, dict) and entry.get("name")
        }
    removed: list[str] = []
    retained: list[str] = []
    forced_terminations: list[dict[str, Any]] = []
    for path in sorted(BROWSER_HOME.glob(f"{session_prefix}*.sock")):
        session_name = path.stem
        if session_name in active_sessions:
            retained.append(str(path))
            continue
        forced_terminations.append(_terminate_session_process(session_name))
        for candidate in _session_runtime_paths(session_name):
            runtime_path = Path(candidate)
            if runtime_path.exists():
                runtime_path.unlink()
                removed.append(str(runtime_path))
    return {
        "sessions_step": sessions_step,
        "active_sessions": sorted(active_sessions),
        "removed_runtime_paths": removed,
        "retained_runtime_paths": retained,
        "forced_terminations": forced_terminations,
    }


def _active_agn_sessions(timeout_seconds: int) -> dict[str, Any]:
    sessions_step = _list_active_sessions(timeout_seconds)
    active_sessions: list[str] = []
    parsed = sessions_step.get("parsed") or {}
    if isinstance(parsed, dict):
        active_sessions = sorted(
            str(entry.get("name", "")).strip()
            for entry in parsed.get("sessions", [])
            if isinstance(entry, dict) and str(entry.get("name", "")).strip().startswith(SESSION_PREFIX)
        )
    return {
        "sessions_step": sessions_step,
        "active_sessions": [name for name in active_sessions if name],
    }


def _base_payload(action: str) -> dict[str, Any]:
    browser_use_bin = _browser_use_bin()
    return {
        "ok": False,
        "wrapper": WRAPPER,
        "action": action,
        "generated_at": utc_now(),
        "input_contract": INPUT_CONTRACT,
        "output_contract": OUTPUT_CONTRACT,
        "authority_boundary": AUTHORITY_BOUNDARY,
        "kill_switch": _kill_switch_state(),
        "system_mode": {
            "mode": _system_mode().get("mode", "unknown"),
            "emergency_stop_active": bool(_system_mode().get("emergency_stop_active", False)),
        },
        "runtime_binary": str(browser_use_bin),
    }


def cmd_run(args: argparse.Namespace) -> int:
    payload = _base_payload("run")
    if payload["kill_switch"]["active"]:
        payload["error"] = "wrapper_disabled"
        print(json.dumps(payload, indent=2))
        return 1
    if payload["system_mode"]["emergency_stop_active"]:
        payload["error"] = "emergency_stop_active"
        print(json.dumps(payload, indent=2))
        return 1

    try:
        url = _validate_url(args.url)
        session = _validate_session(args.session)
        timeout_seconds = _validate_timeout(int(args.timeout_seconds))
        settle_seconds = _validate_settle_seconds(float(args.settle_seconds))
        attachment = _validate_attachment(args.profile, bool(args.connect), args.cdp_url)
        max_active_sessions = _validate_max_active_sessions(int(args.max_active_sessions))
    except ValueError as exc:
        payload["error"] = "invalid_input"
        payload["detail"] = str(exc)
        print(json.dumps(payload, indent=2))
        return 1

    context_args = _browser_use_context_args(bool(args.headed), attachment["profile"], attachment["connect"], attachment["cdp_url"])
    stem = _artifact_stem(args.artifact_stem)
    artifacts = _artifacts(stem)
    payload["session"] = session
    payload["artifacts"] = artifacts
    payload["execution_mode"] = {
        "headed": bool(args.headed),
        "background_expected": not bool(args.headed),
        "attach_mode": "profile" if attachment["profile"] else "connect" if attachment["connect"] else "cdp_url" if attachment["cdp_url"] else "wrapper_owned",
        "profile": attachment["profile"] or None,
        "connect": attachment["connect"],
        "cdp_url": attachment["cdp_url"] or None,
        "keep_session": bool(args.keep_session),
        "settle_seconds": settle_seconds,
    }
    session_budget = _active_agn_sessions(timeout_seconds)
    active_sessions_before_run = session_budget["active_sessions"]
    payload["session_budget"] = {
        "max_active_sessions": max_active_sessions,
        "active_sessions_before_run": active_sessions_before_run,
        "active_session_count_before_run": len(active_sessions_before_run),
        "sessions_probe": session_budget["sessions_step"],
    }
    payload["rollback"] = {
        "command": [
            "python3",
            "scripts/agn_browser_use_wrapper.py",
            "rollback",
            "--session",
            session,
        ],
        "runtime_paths": _session_runtime_paths(session),
    }
    payload["steps"] = []
    sessions_probe = session_budget["sessions_step"]
    if sessions_probe.get("returncode") not in {0, None}:
        payload["budget_notice"] = "active session probe failed; proceeding without session-budget enforcement"
    elif session not in active_sessions_before_run and len(active_sessions_before_run) >= max_active_sessions:
        payload["error"] = "session_budget_exceeded"
        payload["detail"] = (
            f"Refusing to start {session!r}: {len(active_sessions_before_run)} AGN-managed sessions are already active, "
            f"which meets or exceeds the budget of {max_active_sessions}."
        )
        payload["suggested_recovery"] = {
            "close_existing_sessions": active_sessions_before_run,
            "prune_command": ["python3", "scripts/agn_browser_use_wrapper.py", "prune", "--session-prefix", SESSION_PREFIX],
        }
        _write_json(artifacts["report"], payload)
        print(json.dumps(payload, indent=2))
        return 1

    open_step = _run_browser_use(session, timeout_seconds, context_args, "open", url)
    payload["steps"].append({"name": "open", **open_step})
    if open_step["returncode"] != 0:
        close_step = _close_session(session, timeout_seconds, context_args)
        payload["steps"].append({"name": "close_after_open_failure", **close_step})
        payload["forced_cleanup"] = {
            "termination": _terminate_session_process(session),
            "removed_runtime_paths": _cleanup_session_runtime_paths(session),
        }
        _write_json(artifacts["report"], payload)
        print(json.dumps(payload, indent=2))
        return 1

    if settle_seconds > 0:
        time.sleep(settle_seconds)
        payload["steps"].append(
            {
                "name": "settle_wait",
                "duration_seconds": settle_seconds,
                "returncode": 0,
                "stdout": "",
                "stderr": "",
                "parsed": {"slept_seconds": settle_seconds},
            }
        )

    state_step = _run_browser_use(session, timeout_seconds, context_args, "state")
    payload["steps"].append({"name": "state", **state_step})
    if state_step["parsed"] is not None:
        _write_json(artifacts["state"], state_step["parsed"])
    else:
        _write_json(artifacts["state"], {"stdout": state_step["stdout"], "stderr": state_step["stderr"]})

    screenshot_step = _run_browser_use(session, timeout_seconds, context_args, "screenshot", artifacts["screenshot"])
    payload["steps"].append({"name": "screenshot", **screenshot_step})

    close_step: dict[str, Any] | None = None
    if not bool(args.keep_session):
        close_step = _close_session(session, timeout_seconds, context_args)
        payload["steps"].append({"name": "close", **close_step})
        payload["forced_cleanup"] = {
            "termination": _terminate_session_process(session) if close_step["returncode"] != 0 else {"attempted": False, "terminated": False, "reason": "close_succeeded"},
            "removed_runtime_paths": _cleanup_session_runtime_paths(session),
        }
    else:
        payload["forced_cleanup"] = {
            "termination": {"attempted": False, "terminated": False, "reason": "session_kept"},
            "removed_runtime_paths": [],
        }

    payload["ok"] = (
        open_step["returncode"] == 0
        and state_step["returncode"] == 0
        and screenshot_step["returncode"] == 0
        and (close_step is None or close_step["returncode"] == 0)
    )
    payload["evidence"] = {
        "opened_url": url,
        "screenshot_exists": Path(artifacts["screenshot"]).exists(),
        "state_exists": Path(artifacts["state"]).exists(),
        "background_mode": not bool(args.headed),
    }
    _write_json(artifacts["report"], payload)
    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 1


def cmd_rollback(args: argparse.Namespace) -> int:
    payload = _base_payload("rollback")
    try:
        session = _validate_session(args.session)
        timeout_seconds = _validate_timeout(int(args.timeout_seconds))
        attachment = _validate_attachment(args.profile, bool(args.connect), args.cdp_url)
    except ValueError as exc:
        payload["error"] = "invalid_input"
        payload["detail"] = str(exc)
        print(json.dumps(payload, indent=2))
        return 1

    context_args = _browser_use_context_args(bool(args.headed), attachment["profile"], attachment["connect"], attachment["cdp_url"])
    close_step = _close_session(session, timeout_seconds, context_args)
    termination = _terminate_session_process(session) if not _close_step_ok(close_step) else {"attempted": False, "terminated": False, "reason": "close_succeeded"}
    removed_paths = _cleanup_session_runtime_paths(session)

    payload["session"] = session
    payload["steps"] = [{"name": "close", **close_step}]
    payload["removed_runtime_paths"] = removed_paths
    payload["forced_cleanup"] = termination
    payload["ok"] = _close_step_ok(close_step)
    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 1


def cmd_prune(args: argparse.Namespace) -> int:
    payload = _base_payload("prune")
    try:
        timeout_seconds = _validate_timeout(int(args.timeout_seconds))
    except ValueError as exc:
        payload["error"] = "invalid_input"
        payload["detail"] = str(exc)
        print(json.dumps(payload, indent=2))
        return 1

    result = _prune_stale_runtime_paths(timeout_seconds, str(args.session_prefix))
    payload["session_prefix"] = str(args.session_prefix)
    payload["steps"] = [{"name": "sessions", **result["sessions_step"]}]
    payload["active_sessions"] = result["active_sessions"]
    payload["removed_runtime_paths"] = result["removed_runtime_paths"]
    payload["retained_runtime_paths"] = result["retained_runtime_paths"]
    payload["forced_terminations"] = result["forced_terminations"]
    payload["ok"] = result["sessions_step"]["returncode"] == 0
    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Minimal controlled browser-use wrapper for AGN.")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="Open a URL, capture state, and save a screenshot artifact.")
    run_parser.add_argument("--url", default=DEFAULT_URL)
    run_parser.add_argument("--session", default="")
    run_parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    run_parser.add_argument("--artifact-stem", default="")
    run_parser.add_argument("--settle-seconds", type=float, default=DEFAULT_SETTLE_SECONDS)
    run_parser.add_argument("--headed", action="store_true")
    run_parser.add_argument("--profile", default="")
    run_parser.add_argument("--connect", action="store_true")
    run_parser.add_argument("--cdp-url", default="")
    run_parser.add_argument("--keep-session", action="store_true")
    run_parser.add_argument("--max-active-sessions", type=int, default=DEFAULT_MAX_ACTIVE_SESSIONS)
    run_parser.set_defaults(func=cmd_run)

    rollback_parser = sub.add_parser("rollback", help="Close a wrapper session and remove wrapper runtime files.")
    rollback_parser.add_argument("--session", required=True)
    rollback_parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    rollback_parser.add_argument("--headed", action="store_true")
    rollback_parser.add_argument("--profile", default="")
    rollback_parser.add_argument("--connect", action="store_true")
    rollback_parser.add_argument("--cdp-url", default="")
    rollback_parser.set_defaults(func=cmd_rollback)

    prune_parser = sub.add_parser("prune", help="Remove stale browser-use runtime files for AGN-managed sessions.")
    prune_parser.add_argument("--session-prefix", default="agn-")
    prune_parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    prune_parser.set_defaults(func=cmd_prune)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
