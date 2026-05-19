#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = ROOT / "runtime"
OPENCLAW_CONFIG_PATH = Path.home() / ".openclaw" / "openclaw.json"
RESEARCH_PUBLISH_CONFIG_PATH = RUNTIME_DIR / "research_publish_config.json"


def _load_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_research_publish_config() -> dict[str, Any]:
    return _load_json_dict(RESEARCH_PUBLISH_CONFIG_PATH)


def resolve_research_publish_repo_path() -> str:
    env_value = str(
        os.getenv("AGN_RESEARCH_REPO_PATH", "")
        or os.getenv("AGN_DEFAULT_REPO_PATH", "")
    ).strip()
    if env_value:
        return env_value
    payload = load_research_publish_config()
    return str(payload.get("repo_path", "") or "").strip()


def resolve_research_publish_branch() -> str:
    env_value = str(
        os.getenv("AGN_RESEARCH_WORK_BRANCH", "")
        or os.getenv("AGN_DEFAULT_WORK_BRANCH", "")
    ).strip()
    if env_value:
        return env_value
    payload = load_research_publish_config()
    return str(payload.get("work_branch", "") or "main").strip() or "main"


def resolve_research_blog_repo_path() -> str:
    env_value = str(os.getenv("AGN_RESEARCH_BLOG_REPO_PATH", "") or "").strip()
    if env_value:
        return env_value
    payload = load_research_publish_config()
    configured = str(payload.get("blog_repo_path", "") or "").strip()
    if configured:
        return configured
    default_path = Path.home() / "Documents" / "w2026_academica_web"
    return str(default_path) if default_path.exists() else ""


def resolve_research_blog_branch() -> str:
    env_value = str(os.getenv("AGN_RESEARCH_BLOG_BRANCH", "") or "").strip()
    if env_value:
        return env_value
    payload = load_research_publish_config()
    return str(payload.get("blog_work_branch", "") or "main").strip() or "main"


def resolve_research_blog_science_dir() -> str:
    env_value = str(os.getenv("AGN_RESEARCH_BLOG_SCIENCE_DIR", "") or "").strip()
    if env_value:
        return env_value
    payload = load_research_publish_config()
    return str(payload.get("blog_science_dir", "") or "content/AGNResearch").strip() or "content/AGNResearch"


def load_openclaw_config() -> dict[str, Any]:
    return _load_json_dict(OPENCLAW_CONFIG_PATH)


def resolve_telegram_bot_token(*, account_id: str = "coordinator") -> str:
    env_value = str(os.getenv("TELEGRAM_BOT_TOKEN", "") or "").strip()
    if env_value:
        return env_value
    payload = load_openclaw_config()
    channels = payload.get("channels", {})
    if not isinstance(channels, dict):
        return ""
    telegram = channels.get("telegram", {})
    if not isinstance(telegram, dict):
        return ""
    accounts = telegram.get("accounts", {})
    if not isinstance(accounts, dict):
        return ""
    preferred = str(account_id or "").strip()
    for candidate in (
        preferred,
        str(telegram.get("defaultAccount", "") or "").strip(),
        "coordinator",
    ):
        if not candidate:
            continue
        spec = accounts.get(candidate, {})
        if not isinstance(spec, dict):
            continue
        token = str(spec.get("botToken", "") or spec.get("token", "")).strip()
        if token:
            return token
    return ""
