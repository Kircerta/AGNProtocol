#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "providers.json"
CAPABILITIES_PATH = ROOT / "runtime" / "provider_capabilities.json"

DEFAULT_REGISTRY: dict[str, Any] = {
    "version": 1,
    "default_executor": "codex",
    "default_reviewer": "gemini",
    "executors": {
        "codex": {
            "kind": "cli",
            "command": "codex",
            "description": "Codex CLI executor",
        },
        "gemini": {
            "kind": "cli",
            "command": "gemini",
            "description": "Gemini CLI executor",
        },
        "claude": {
            "kind": "cli",
            "command": "claude",
            "commands": ["claude", "claude-code"],
            "description": "Claude Code CLI executor",
        },
        "qwen_local": {
            "kind": "api",
            "base_url_env": "QWEN_LOCAL_BASE_URL",
            "model_env": "QWEN_LOCAL_MODEL",
            "default_base_url": "http://127.0.0.1:8765/v1",
            "default_model": "qwen-local-model",
            "requires_api_key": False,
            "description": "Local MLX Qwen worker for bounded low-risk structured tasks",
        },
    },
    "reviewers": {
        "codex": {
            "kind": "cli",
            "command": "codex",
            "description": "Codex CLI reviewer",
        },
        "gemini": {
            "kind": "cli",
            "command": "gemini",
            "description": "Gemini CLI reviewer",
        },
        "claude": {
            "kind": "cli",
            "command": "claude",
            "commands": ["claude", "claude-code"],
            "description": "Claude Code CLI reviewer",
        },
        "deepseek": {
            "kind": "api",
            "base_url_env": "DEEPSEEK_BASE_URL",
            "api_key_env": "DEEPSEEK_API_KEY",
            "model_env": "DEEPSEEK_MODEL",
            "default_base_url": "https://api.deepseek.com/v1",
            "default_model": "deepseek-chat",
            "description": "DeepSeek OpenAI-compatible reviewer API",
        },
        "qwen_local": {
            "kind": "api",
            "base_url_env": "QWEN_LOCAL_BASE_URL",
            "model_env": "QWEN_LOCAL_MODEL",
            "default_base_url": "http://127.0.0.1:8765/v1",
            "default_model": "qwen-local-model",
            "requires_api_key": False,
            "description": "Local MLX Qwen worker for bounded low-risk structured tasks",
        },
    },
}


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def load_registry(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return dict(DEFAULT_REGISTRY)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(DEFAULT_REGISTRY)
    if not isinstance(payload, dict):
        return dict(DEFAULT_REGISTRY)
    merged = dict(DEFAULT_REGISTRY)
    merged.update(payload)
    for key in ("executors", "reviewers"):
        base = DEFAULT_REGISTRY.get(key, {})
        custom = payload.get(key, {})
        if isinstance(base, dict) and isinstance(custom, dict):
            merged[key] = {**base, **custom}
    return merged


def resolve_executor_provider(raw: str, registry: dict[str, Any] | None = None) -> str:
    reg = registry or load_registry()
    executors = reg.get("executors", {})
    default = str(reg.get("default_executor", "codex")).strip().lower() or "codex"
    candidate = str(raw or "").strip().lower() or default
    if isinstance(executors, dict) and candidate in executors:
        return candidate
    return default


def resolve_reviewer_provider(raw: str, registry: dict[str, Any] | None = None) -> str:
    reg = registry or load_registry()
    reviewers = reg.get("reviewers", {})
    default = str(reg.get("default_reviewer", "gemini")).strip().lower() or "gemini"
    candidate = str(raw or "").strip().lower() or default
    if isinstance(reviewers, dict) and candidate in reviewers:
        return candidate
    return default


def _probe_cli(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    cmd = str(spec.get("command", "")).strip()
    commands_raw = spec.get("commands")
    candidates: list[str] = []
    if isinstance(commands_raw, list):
        for item in commands_raw:
            c = str(item or "").strip()
            if c:
                candidates.append(c)
    if cmd and cmd not in candidates:
        candidates.insert(0, cmd)

    selected = ""
    path = None
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            selected = candidate
            path = found
            break
    if not selected and cmd:
        selected = cmd
    return {
        "name": name,
        "kind": "cli",
        "command": selected,
        "command_candidates": candidates,
        "available": bool(path),
        "path": path or "",
    }


def _probe_api(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    api_key_env = str(spec.get("api_key_env", "")).strip()
    base_url_env = str(spec.get("base_url_env", "")).strip()
    model_env = str(spec.get("model_env", "")).strip()
    api_key = str(os.getenv(api_key_env, "")).strip() if api_key_env else ""
    base_url = str(os.getenv(base_url_env, "")).strip() if base_url_env else ""
    model = str(os.getenv(model_env, "")).strip() if model_env else ""
    requires_api_key = bool(spec.get("requires_api_key", bool(api_key_env)))
    if not base_url:
        base_url = str(spec.get("default_base_url", "")).strip()
    if not model:
        model = str(spec.get("default_model", "")).strip()
    auth_ready = bool(api_key) if requires_api_key else True
    model_path_exists = True
    storage_ready = True
    unavailable_reason = ""
    if name == "qwen_local" and model:
        model_path = Path(os.path.expanduser(model))
        if model_path.is_absolute():
            model_path_exists = model_path.exists()
            model_readable = os.access(model_path, os.R_OK) if model_path_exists else False
            if not model_path_exists:
                unavailable_reason = f"qwen_model_path_missing:{model}"
            elif not model_readable:
                unavailable_reason = f"qwen_model_path_not_readable:{model}"
                model_path_exists = False  # treat unreadable as unavailable
        if not unavailable_reason and _is_local_base_url(base_url):
            reachable, reason = _check_local_api_endpoint(base_url)
            if not reachable:
                unavailable_reason = reason
    elif requires_api_key and not api_key:
        unavailable_reason = f"provider_api_key_missing:{api_key_env or 'api_key'}"
    elif not base_url or not model:
        unavailable_reason = "provider_missing_base_url_or_model"
    elif _is_local_base_url(base_url):
        reachable, reason = _check_local_api_endpoint(base_url)
        if not reachable:
            unavailable_reason = reason
    return {
        "name": name,
        "kind": "api",
        "available": bool(auth_ready and base_url and model and model_path_exists and storage_ready and not unavailable_reason),
        "api_key_env": api_key_env,
        "base_url_env": base_url_env,
        "model_env": model_env,
        "base_url": base_url,
        "model": model,
        "requires_api_key": requires_api_key,
        "has_api_key": bool(api_key),
        "model_path_exists": model_path_exists,
        "storage_ready": storage_ready,
        "unavailable_reason": unavailable_reason,
    }


def _is_local_base_url(base_url: str) -> bool:
    try:
        parsed = urlparse(base_url)
    except Exception:
        return False
    return (parsed.hostname or "").strip().lower() in {"127.0.0.1", "localhost", "::1"}


def _check_local_api_endpoint(base_url: str) -> tuple[bool, str]:
    try:
        parsed = urlparse(base_url)
    except Exception:
        return False, "local_provider_endpoint_unparseable"

    host = (parsed.hostname or "").strip()
    scheme = (parsed.scheme or "http").strip().lower()
    port = parsed.port or (443 if scheme == "https" else 80)
    if not host:
        return False, "local_provider_endpoint_unparseable"

    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True, ""
    except OSError:
        return False, f"local_provider_endpoint_unreachable:{host}:{port}"


def probe_capabilities(registry: dict[str, Any] | None = None) -> dict[str, Any]:
    reg = registry or load_registry()
    executors = reg.get("executors", {})
    reviewers = reg.get("reviewers", {})
    payload: dict[str, Any] = {
        "generated_at": utc_now_iso(),
        "registry_version": int(reg.get("version", 1) or 1),
        "default_executor": resolve_executor_provider("", reg),
        "default_reviewer": resolve_reviewer_provider("", reg),
        "executors": {},
        "reviewers": {},
    }

    if isinstance(executors, dict):
        for name, spec in sorted(executors.items()):
            if not isinstance(spec, dict):
                continue
            kind = str(spec.get("kind", "cli")).strip().lower()
            probe = _probe_cli(name, spec) if kind == "cli" else _probe_api(name, spec)
            payload["executors"][name] = probe

    if isinstance(reviewers, dict):
        for name, spec in sorted(reviewers.items()):
            if not isinstance(spec, dict):
                continue
            kind = str(spec.get("kind", "cli")).strip().lower()
            probe = _probe_cli(name, spec) if kind == "cli" else _probe_api(name, spec)
            payload["reviewers"][name] = probe

    return payload


def cmd_probe(args: argparse.Namespace) -> int:
    reg = load_registry(Path(args.registry) if args.registry else CONFIG_PATH)
    capabilities = probe_capabilities(reg)
    output = Path(args.output) if args.output else CAPABILITIES_PATH
    atomic_write_json(output, capabilities)
    print(
        json.dumps(
            {
                "ok": True,
                "output": str(output),
                "default_executor": capabilities["default_executor"],
                "default_reviewer": capabilities["default_reviewer"],
            },
            ensure_ascii=True,
        )
    )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    reg = load_registry(Path(args.registry) if args.registry else CONFIG_PATH)
    print(json.dumps(reg, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AGN provider registry + capabilities probe")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_probe = sub.add_parser("probe")
    p_probe.add_argument("--registry", default="")
    p_probe.add_argument("--output", default="")

    p_show = sub.add_parser("show")
    p_show.add_argument("--registry", default="")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.cmd == "probe":
        return cmd_probe(args)
    return cmd_show(args)


if __name__ == "__main__":
    raise SystemExit(main())
