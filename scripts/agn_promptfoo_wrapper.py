#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
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
WRAPPER = "promptfoo"
DISABLE_ENV = "AGN_PROMPTFOO_WRAPPER_DISABLED"
DEFAULT_TIMEOUT_SECONDS = 300
STEM_RE = re.compile(r"[^A-Za-z0-9._-]")

INPUT_CONTRACT = {
    "command": "run",
    "required": [],
    "optional": ["config", "prompt", "word", "expected", "timeout_seconds", "artifact_stem"],
    "constraints": {
        "config": "optional path inside the AGN repo; if omitted the wrapper generates an echo-provider smoke config",
        "prompt": "used only when config is omitted",
        "word": "used only when config is omitted",
        "expected": "used only when config is omitted",
        "timeout_seconds": "integer between 10 and 900",
    },
}
OUTPUT_CONTRACT = {
    "fields": [
        "ok",
        "wrapper",
        "action",
        "artifacts",
        "authority_boundary",
        "kill_switch",
        "rollback",
        "steps",
    ],
    "artifacts": {
        "report": "Promptfoo JSON eval report",
        "config": "YAML config used for the eval",
        "log": "captured stdout/stderr from promptfoo",
    },
}
AUTHORITY_BOUNDARY = {
    "scope": "evaluation-only wrapper around promptfoo",
    "allowed_commands": ["npx --yes promptfoo@latest eval"],
    "prohibited_capabilities": [
        "no governance or control-plane writes",
        "no arbitrary shell execution beyond promptfoo eval",
        "no config paths outside the AGN repo",
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


def _validate_timeout(timeout_seconds: int) -> int:
    if timeout_seconds < 10 or timeout_seconds > 900:
        raise ValueError("timeout_seconds must be between 10 and 900")
    return timeout_seconds


def _artifact_stem(explicit: str | None) -> str:
    if explicit:
        return STEM_RE.sub("-", explicit).strip("-._") or "promptfoo-wrapper"
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"agn_promptfoo_wrapper_{stamp}"


def _artifacts(stem: str) -> dict[str, str]:
    return {
        "report": str(REPORTS_DIR / f"{stem}.json"),
        "config": str(REPORTS_DIR / f"{stem}.config.yaml"),
        "log": str(REPORTS_DIR / f"{stem}.log"),
    }


def _write_text(path: str | Path, content: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _write_json(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def _generated_config(prompt: str, word: str, expected: str) -> str:
    return (
        "description: AGN promptfoo wrapper smoke\n"
        "prompts:\n"
        f"  - {prompt!r}\n"
        "providers:\n"
        "  - echo\n"
        "tests:\n"
        "  - vars:\n"
        f"      word: {word!r}\n"
        "    assert:\n"
        "      - type: equals\n"
        f"        value: {expected!r}\n"
    )


def _validate_config_path(config: str | None) -> str | None:
    if not config:
        return None
    path = Path(config).expanduser().resolve()
    try:
        path.relative_to(ROOT)
    except ValueError as exc:
        raise ValueError("config must stay inside the AGN repo root") from exc
    if not path.exists():
        raise ValueError("config path does not exist")
    return str(path)


def _run_promptfoo(config_path: str, report_path: str, timeout_seconds: int) -> dict[str, Any]:
    command = ["npx", "--yes", "promptfoo@latest", "eval", "-c", config_path, "-o", report_path]
    started = time.time()
    proc = subprocess.run(
        command,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    return {
        "command": command,
        "returncode": proc.returncode,
        "duration_seconds": round(time.time() - started, 3),
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
    }


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
        timeout_seconds = _validate_timeout(int(args.timeout_seconds))
        config_path = _validate_config_path(args.config)
    except ValueError as exc:
        payload["error"] = "invalid_input"
        payload["detail"] = str(exc)
        print(json.dumps(payload, indent=2))
        return 1

    stem = _artifact_stem(args.artifact_stem)
    artifacts = _artifacts(stem)
    generated_config = False
    if config_path is None:
        prompt = str(args.prompt).strip() or "Return exactly: {{word}}"
        word = str(args.word).strip() or "hello"
        expected = str(args.expected).strip() or f"Return exactly: {word}"
        _write_text(artifacts["config"], _generated_config(prompt, word, expected))
        config_path = artifacts["config"]
        generated_config = True
    else:
        _write_text(artifacts["config"], Path(config_path).read_text(encoding="utf-8"))

    step = _run_promptfoo(config_path, artifacts["report"], timeout_seconds)
    _write_text(artifacts["log"], f"{step['stdout']}\n\n{step['stderr']}".strip() + "\n")

    payload["artifacts"] = artifacts
    payload["generated_config"] = generated_config
    payload["rollback"] = {
        "command": ["python3", "scripts/agn_promptfoo_wrapper.py", "rollback", "--artifact-stem", stem],
        "runtime_paths": [artifacts["config"], artifacts["log"]],
    }
    payload["steps"] = [{"name": "promptfoo_eval", **step}]
    payload["ok"] = step["returncode"] == 0 and Path(artifacts["report"]).exists()
    if Path(artifacts["report"]).exists():
        try:
            payload["report_preview"] = json.loads(Path(artifacts["report"]).read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload["report_preview"] = {"error": "report_not_json"}
    _write_json(artifacts["report"] + ".wrapper.json", payload)
    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 1


def cmd_rollback(args: argparse.Namespace) -> int:
    payload = _base_payload("rollback")
    stem = _artifact_stem(args.artifact_stem)
    artifacts = _artifacts(stem)
    removed_paths: list[str] = []
    for key in ("config", "log"):
        path = Path(artifacts[key])
        if path.exists():
            path.unlink()
            removed_paths.append(str(path))
    if args.remove_report:
        report_path = Path(artifacts["report"])
        if report_path.exists():
            report_path.unlink()
            removed_paths.append(str(report_path))
        wrapper_report = Path(artifacts["report"] + ".wrapper.json")
        if wrapper_report.exists():
            wrapper_report.unlink()
            removed_paths.append(str(wrapper_report))
    payload["artifacts"] = artifacts
    payload["removed_paths"] = removed_paths
    payload["ok"] = True
    print(json.dumps(payload, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Minimal controlled promptfoo wrapper for AGN.")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="Run a minimal promptfoo eval and save inspectable artifacts.")
    run_parser.add_argument("--config", default="")
    run_parser.add_argument("--prompt", default="Return exactly: {{word}}")
    run_parser.add_argument("--word", default="hello")
    run_parser.add_argument("--expected", default="")
    run_parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    run_parser.add_argument("--artifact-stem", default="")
    run_parser.set_defaults(func=cmd_run)

    rollback_parser = sub.add_parser("rollback", help="Remove wrapper-generated promptfoo config/log artifacts.")
    rollback_parser.add_argument("--artifact-stem", required=True)
    rollback_parser.add_argument("--remove-report", action="store_true")
    rollback_parser.set_defaults(func=cmd_rollback)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
