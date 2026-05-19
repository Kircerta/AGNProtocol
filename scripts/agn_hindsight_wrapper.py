#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from emergency_stop import load_system_mode
except ImportError:  # pragma: no cover
    from scripts.emergency_stop import load_system_mode

ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = ROOT / "reports"
WRAPPER = "hindsight"
DISABLE_ENV = "AGN_HINDSIGHT_WRAPPER_DISABLED"
PROFILE_PREFIX = "agn-hindsight-wrapper-"
TOKEN_RE = re.compile(r"^[A-Za-z0-9._:-]{3,128}$")
DEFAULT_TIMEOUT_SECONDS = 600
VENV_ROOT = Path.home() / ".agn_external_wrappers_venv"
HINDSIGHT_BIN = VENV_ROOT / "bin" / "hindsight-embed"
HINDSIGHT_HOME = Path.home() / ".hindsight"

INPUT_CONTRACT = {
    "command": "run",
    "required": ["memory_text", "recall_query"],
    "optional": ["profile", "bank_id", "provider", "model", "timeout_seconds", "artifact_stem"],
    "constraints": {
        "profile": f"must match {TOKEN_RE.pattern} and start with {PROFILE_PREFIX!r} when supplied",
        "bank_id": f"must match {TOKEN_RE.pattern}",
        "provider": "must be one of: gemini, openai, groq, ollama, vertexai, mock",
        "model": "non-empty provider-compatible model name",
        "timeout_seconds": "integer between 30 and 1800",
    },
}
OUTPUT_CONTRACT = {
    "fields": [
        "ok",
        "wrapper",
        "action",
        "profile",
        "bank_id",
        "artifacts",
        "authority_boundary",
        "kill_switch",
        "rollback",
        "steps",
    ],
    "artifacts": {
        "report": "JSON report with retain/recall/status/stop evidence",
        "daemon_log": "daemon log path if the wrapper profile produced one",
    },
}
AUTHORITY_BOUNDARY = {
    "scope": "append-only memory helper built on hindsight-embed",
    "allowed_commands": ["memory retain", "memory recall", "daemon status", "daemon stop"],
    "prohibited_capabilities": [
        "no writes into AGN canonical memory files",
        "no profile takeover outside wrapper-owned profile prefix",
        "no dispatcher or governance mutations",
        "no active-profile changes",
    ],
}
SUPPORTED_PROVIDERS = {"gemini", "openai", "groq", "ollama", "vertexai", "mock"}


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _kill_switch_state() -> dict[str, Any]:
    return {
        "env_var": DISABLE_ENV,
        "active": os.getenv(DISABLE_ENV, "").strip().lower() in {"1", "true", "yes", "on"},
    }


def _system_mode() -> dict[str, Any]:
    return load_system_mode()


def _validate_token(value: str, *, label: str, prefix: str = "") -> str:
    if not TOKEN_RE.match(value):
        raise ValueError(f"{label} does not match the allowed pattern")
    if prefix and not value.startswith(prefix):
        raise ValueError(f"{label} must start with {prefix!r}")
    return value


def _validate_timeout(timeout_seconds: int) -> int:
    if timeout_seconds < 30 or timeout_seconds > 1800:
        raise ValueError("timeout_seconds must be between 30 and 1800")
    return timeout_seconds


def _default_profile(explicit: str | None) -> str:
    if explicit:
        return _validate_token(explicit, label="profile", prefix=PROFILE_PREFIX)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{PROFILE_PREFIX}{stamp}"


def _artifact_stem(explicit: str | None) -> str:
    if explicit:
        return re.sub(r"[^A-Za-z0-9._-]", "-", explicit).strip("-._") or "hindsight-wrapper"
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"agn_hindsight_wrapper_{stamp}"


def _artifacts(stem: str, profile: str) -> dict[str, str]:
    return {
        "report": str(REPORTS_DIR / f"{stem}.json"),
        "daemon_log": str(HINDSIGHT_HOME / "profiles" / f"{profile}.log"),
    }


def _write_json(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def _api_key_for_provider(provider: str) -> str:
    env_map = {
        "gemini": os.getenv("HINDSIGHT_API_LLM_API_KEY") or os.getenv("GOOGLE_API_KEY"),
        "openai": os.getenv("HINDSIGHT_API_LLM_API_KEY") or os.getenv("OPENAI_API_KEY"),
        "groq": os.getenv("HINDSIGHT_API_LLM_API_KEY") or os.getenv("GROQ_API_KEY"),
        "ollama": os.getenv("HINDSIGHT_API_LLM_API_KEY", ""),
        "vertexai": os.getenv("HINDSIGHT_API_LLM_API_KEY", ""),
        "mock": os.getenv("HINDSIGHT_API_LLM_API_KEY", "dummy"),
    }
    return str(env_map.get(provider, "")).strip()


def _env_for_run(provider: str, model: str, profile: str) -> dict[str, str]:
    api_key = _api_key_for_provider(provider)
    if provider not in {"ollama", "vertexai", "mock"} and not api_key:
        raise ValueError(f"missing API key for provider {provider!r}")
    env = os.environ.copy()
    env["HINDSIGHT_API_LLM_PROVIDER"] = provider
    env["HINDSIGHT_API_LLM_MODEL"] = model
    env["HINDSIGHT_EMBED_PROFILE"] = profile
    if api_key:
        env["HINDSIGHT_API_LLM_API_KEY"] = api_key
    return env


def _run_hindsight(env: dict[str, str], timeout_seconds: int, *args: str) -> dict[str, Any]:
    if not HINDSIGHT_BIN.exists():
        raise FileNotFoundError(f"hindsight-embed binary not found at {HINDSIGHT_BIN}")
    command = [str(HINDSIGHT_BIN), "-p", env["HINDSIGHT_EMBED_PROFILE"], *args]
    started = time.time()
    proc = subprocess.run(
        command,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        env=env,
        check=False,
    )
    stdout = (proc.stdout or "").strip()
    parsed: dict[str, Any] | list[Any] | None = None
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


def _tail_log(profile: str, lines: int = 80) -> str:
    path = HINDSIGHT_HOME / "profiles" / f"{profile}.log"
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(text[-lines:])


def _base_payload(action: str) -> dict[str, Any]:
    system_mode = _system_mode()
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
            "mode": system_mode.get("mode", "unknown"),
            "emergency_stop_active": bool(system_mode.get("emergency_stop_active", False)),
        },
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
        provider = str(args.provider).strip()
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(f"provider must be one of {sorted(SUPPORTED_PROVIDERS)}")
        model = str(args.model).strip()
        if not model:
            raise ValueError("model must be non-empty")
        profile = _default_profile(args.profile)
        bank_id = _validate_token(args.bank_id, label="bank_id")
        timeout_seconds = _validate_timeout(int(args.timeout_seconds))
        env = _env_for_run(provider, model, profile)
    except ValueError as exc:
        payload["error"] = "invalid_input"
        payload["detail"] = str(exc)
        print(json.dumps(payload, indent=2))
        return 1

    stem = _artifact_stem(args.artifact_stem)
    artifacts = _artifacts(stem, profile)
    payload["profile"] = profile
    payload["bank_id"] = bank_id
    payload["artifacts"] = artifacts
    payload["rollback"] = {
        "command": [
            "python3",
            "scripts/agn_hindsight_wrapper.py",
            "rollback",
            "--profile",
            profile,
        ],
        "runtime_paths": [
            str(HINDSIGHT_HOME / "profiles" / f"{profile}.log"),
            str(HINDSIGHT_HOME / "profiles" / f"{profile}.lock"),
            str(HINDSIGHT_HOME / "profiles" / f"{profile}.env"),
        ],
    }
    payload["steps"] = []

    retain_step = _run_hindsight(env, timeout_seconds, "memory", "retain", bank_id, args.memory_text)
    payload["steps"].append({"name": "retain", **retain_step})

    recall_step = _run_hindsight(env, timeout_seconds, "memory", "recall", bank_id, args.recall_query, "-o", "json")
    payload["steps"].append({"name": "recall", **recall_step})

    status_step = _run_hindsight(env, timeout_seconds, "daemon", "status")
    payload["steps"].append({"name": "daemon_status", **status_step})

    stop_step = _run_hindsight(env, timeout_seconds, "daemon", "stop")
    payload["steps"].append({"name": "daemon_stop", **stop_step})

    payload["daemon_log_tail"] = _tail_log(profile)
    payload["ok"] = (
        retain_step["returncode"] == 0
        and recall_step["returncode"] == 0
        and status_step["returncode"] == 0
        and stop_step["returncode"] == 0
    )
    _write_json(artifacts["report"], payload)
    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 1


def cmd_rollback(args: argparse.Namespace) -> int:
    payload = _base_payload("rollback")
    try:
        profile = _validate_token(args.profile, label="profile", prefix=PROFILE_PREFIX)
        timeout_seconds = _validate_timeout(int(args.timeout_seconds))
    except ValueError as exc:
        payload["error"] = "invalid_input"
        payload["detail"] = str(exc)
        print(json.dumps(payload, indent=2))
        return 1

    env = os.environ.copy()
    env["HINDSIGHT_EMBED_PROFILE"] = profile
    stop_step = _run_hindsight(env, timeout_seconds, "daemon", "stop")
    removed_paths: list[str] = []
    for suffix in (".log", ".lock", ".env"):
        path = HINDSIGHT_HOME / "profiles" / f"{profile}{suffix}"
        if path.exists():
            path.unlink()
            removed_paths.append(str(path))

    payload["profile"] = profile
    payload["steps"] = [{"name": "daemon_stop", **stop_step}]
    payload["removed_runtime_paths"] = removed_paths
    payload["ok"] = stop_step["returncode"] == 0
    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Minimal controlled hindsight wrapper for AGN.")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="Retain and recall one memory under a wrapper-owned profile.")
    run_parser.add_argument("--memory-text", default="AGN wrapper smoke memory")
    run_parser.add_argument("--recall-query", default="AGN wrapper smoke memory")
    run_parser.add_argument("--profile", default="")
    run_parser.add_argument("--bank-id", default="agn-wrapper-smoke")
    run_parser.add_argument("--provider", default="mock")
    run_parser.add_argument("--model", default="mock-model")
    run_parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    run_parser.add_argument("--artifact-stem", default="")
    run_parser.set_defaults(func=cmd_run)

    rollback_parser = sub.add_parser("rollback", help="Stop a wrapper profile daemon and remove wrapper runtime files.")
    rollback_parser.add_argument("--profile", required=True)
    rollback_parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    rollback_parser.set_defaults(func=cmd_rollback)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
