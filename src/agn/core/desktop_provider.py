"""AGN Desktop Control Provider Resolution.

Centralizes resolution of the desktop control binary path so that all AGN
modules use one configurable source instead of hardcoding gui-agent paths.

The binary can be overridden via:
  1. AGN_DESKTOP_CONTROL_BIN environment variable (highest priority)
  2. config/desktop_control.json "binary" field
  3. Default example path: /example/tools/gui-agent

This supports future migration from gui-agent (OpenClaw) to Hermes or any
other desktop control binary without changing calling code.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


PACKAGE_PATH = "agn.core.desktop_provider"

_DEFAULT_BIN = Path("/example/tools/gui-agent")
_CONFIG_REL = Path("config") / "desktop_control.json"

_cached_bin: Path | None = None
_cached_config_mtime: float = 0.0


def _repo_root() -> Path:
    """Resolve AGN repo root without importing admin_control to avoid cycles."""
    override = os.environ.get("AGN_REPO_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    # Walk up from src/agn/core/desktop_provider.py → src/agn/core → src/agn → src → repo
    return Path(__file__).resolve().parents[3]


def _load_config_binary() -> str:
    """Read the binary path from config/desktop_control.json if it exists."""
    config_path = _repo_root() / _CONFIG_REL
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return str(data.get("binary", "")).strip()
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    return ""


def get_desktop_control_bin() -> Path:
    """Resolve the desktop control binary path.

    Priority order:
      1. AGN_DESKTOP_CONTROL_BIN environment variable
      2. config/desktop_control.json "binary" field
      3. Default example path: /example/tools/gui-agent

    Returns:
        Resolved Path to the desktop control binary.
    """
    global _cached_bin, _cached_config_mtime

    # Environment variable always wins — no caching needed
    env_override = os.environ.get("AGN_DESKTOP_CONTROL_BIN", "").strip()
    if env_override:
        return Path(env_override).expanduser().resolve()

    # Check config file (with mtime-based caching)
    config_path = _repo_root() / _CONFIG_REL
    try:
        mtime = config_path.stat().st_mtime
    except OSError:
        mtime = 0.0

    if _cached_bin is not None and mtime == _cached_config_mtime:
        return _cached_bin

    config_binary = _load_config_binary()
    if config_binary:
        _cached_bin = Path(config_binary).expanduser().resolve()
    else:
        _cached_bin = _DEFAULT_BIN

    _cached_config_mtime = mtime
    return _cached_bin


def get_provider_info() -> dict[str, Any]:
    """Return metadata about the current desktop control provider.

    Useful for capability snapshots, preflight checks, and debugging.
    """
    bin_path = get_desktop_control_bin()
    return {
        "binary": str(bin_path),
        "exists": bin_path.exists(),
        "source": _resolve_source(),
        "default_binary": str(_DEFAULT_BIN),
        "provider_name": bin_path.stem,
    }


def _resolve_source() -> str:
    """Determine which source provided the current binary path."""
    if os.environ.get("AGN_DESKTOP_CONTROL_BIN", "").strip():
        return "environment:AGN_DESKTOP_CONTROL_BIN"
    config_binary = _load_config_binary()
    if config_binary:
        return "config:desktop_control.json"
    return "default"
