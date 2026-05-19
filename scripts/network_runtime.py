#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from agent_runner import append_audit
from provider_registry import load_registry, probe_capabilities
try:
    from research_runtime import (
        resolve_research_blog_branch,
        resolve_research_blog_repo_path,
        resolve_research_blog_science_dir,
        resolve_research_publish_branch,
        resolve_research_publish_repo_path,
    )
except ImportError:  # pragma: no cover - package import fallback
    from scripts.research_runtime import (
        resolve_research_blog_branch,
        resolve_research_blog_repo_path,
        resolve_research_blog_science_dir,
        resolve_research_publish_branch,
        resolve_research_publish_repo_path,
    )

RUNTIME_DIR = ROOT / "runtime"
AUTONOMY_CONFIG_PATH = RUNTIME_DIR / "research_autonomy_config.json"
RUNTIME_STATE_PATH = RUNTIME_DIR / "network_runtime_state.json"
BRIEFING_JSON_PATH = RUNTIME_DIR / "coordinator_network_briefing.json"
BRIEFING_MD_PATH = RUNTIME_DIR / "coordinator_network_briefing.md"
CHANGE_EVENT_PATH = RUNTIME_DIR / "coordinator_change_event.json"
CHANGE_EVENT_HISTORY_PATH = RUNTIME_DIR / "coordinator_change_events.jsonl"
DUTY_REFRESH_PATH = RUNTIME_DIR / "coordinator_duty_refresh.json"
REFRESH_MESSAGE_PATH = RUNTIME_DIR / "coordinator_refresh_message.txt"
FIRST_TEST_MESSAGE_PATH = RUNTIME_DIR / "coordinator_first_research_test.txt"

DEFAULT_AUTONOMY_CONFIG: dict[str, Any] = {
    "auto_enabled": True,
    "morning_window": "09:00",
    "afternoon_window": "15:00",
}

PRIMARY_SURFACES = [
    "scripts/event_sourcing.py",
    "scripts/coordinator_heartbeat.py",
    "agn_api/main.py",
    "agn_api/ssot_store.py",
    "agn_api/task_engine.py",
    "static/agn_console.html",
]
SECONDARY_SURFACES = [
    "scripts/coordinator_ingest.py",
    "scripts/run_agn_task.py",
    "scripts/coordinator_loop.py",
    "scripts/executor_worker.py",
    "scripts/reviewer_worker.py",
]
LEGACY_SURFACES = [
    "legacy dashboard removed",
    "thin wrappers deweighted under scripts/validation/",
]
TELEGRAM_COMMANDS = [
    "/agn help",
    "/agn status",
    "/agn costs",
    "/research start",
    "/research status",
    "/research pause",
    "/research fallback",
    "/research mark-exception",
    "/research windows",
    "/research set-morning HH:MM",
    "/research set-afternoon HH:MM",
    "/research auto on",
    "/research auto off",
]
RELEASE_SUMMARY = [
    "research_worker switched from deterministic stub to real CLI provider adapter with stub fallback for validation",
    "Telegram is now a real research management entrypoint instead of a notify-only surface",
    "manual research start now supports guided question/hypothesis intake plus a minimal safe-start mode",
    "automatic daily research now sends a morning brief and only enters afternoon autonomy if the admin does not override",
    "daily research auto-run is fixed to two configurable windows with on/off control",
    "daily research now hard-blocks incomplete contract ingress instead of logging-and-continuing",
    "research completion now requires publish receipt plus Telegram delivery receipt instead of stopping at results/verdict",
    "trusted dependency installs from official sources are allowed during execution and must be logged",
    "Coordinator briefing, change event, and duty refresh files are now published in runtime/",
]


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _load_json_or_default(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(default)
    if isinstance(payload, dict):
        return payload
    return dict(default)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def load_autonomy_config() -> dict[str, Any]:
    payload = _load_json_or_default(AUTONOMY_CONFIG_PATH, DEFAULT_AUTONOMY_CONFIG)
    clean: dict[str, Any] = dict(DEFAULT_AUTONOMY_CONFIG)
    clean["auto_enabled"] = bool(payload.get("auto_enabled", DEFAULT_AUTONOMY_CONFIG["auto_enabled"]))
    for key in ("morning_window", "afternoon_window"):
        value = str(payload.get(key, DEFAULT_AUTONOMY_CONFIG[key]) or DEFAULT_AUTONOMY_CONFIG[key]).strip()
        clean[key] = value if _is_valid_hhmm(value) else DEFAULT_AUTONOMY_CONFIG[key]
    return clean


def save_autonomy_config(payload: dict[str, Any]) -> dict[str, Any]:
    merged = load_autonomy_config()
    if "auto_enabled" in payload:
        merged["auto_enabled"] = bool(payload.get("auto_enabled"))
    for key in ("morning_window", "afternoon_window"):
        if key in payload:
            value = str(payload.get(key, "")).strip()
            if _is_valid_hhmm(value):
                merged[key] = value
    _atomic_write_json(AUTONOMY_CONFIG_PATH, merged)
    return merged


def effective_windows(config: dict[str, Any] | None = None) -> list[str]:
    payload = config if isinstance(config, dict) else load_autonomy_config()
    windows = [
        str(payload.get("morning_window", DEFAULT_AUTONOMY_CONFIG["morning_window"])).strip(),
        str(payload.get("afternoon_window", DEFAULT_AUTONOMY_CONFIG["afternoon_window"])).strip(),
    ]
    return [window for window in windows if _is_valid_hhmm(window)]


def _is_valid_hhmm(value: str) -> bool:
    try:
        datetime.strptime(str(value or "").strip(), "%H:%M")
    except ValueError:
        return False
    return True


def _provider_summary() -> dict[str, Any]:
    caps_path = RUNTIME_DIR / "provider_capabilities.json"
    if caps_path.exists():
        payload = _load_json_or_default(caps_path, {})
    else:
        payload = probe_capabilities(load_registry())
    executors = payload.get("executors", {}) if isinstance(payload.get("executors"), dict) else {}
    reviewers = payload.get("reviewers", {}) if isinstance(payload.get("reviewers"), dict) else {}
    return {
        "default_executor": str(payload.get("default_executor", "codex")).strip() or "codex",
        "default_reviewer": str(payload.get("default_reviewer", "gemini")).strip() or "gemini",
        "executors_available": [name for name, spec in sorted(executors.items()) if isinstance(spec, dict) and bool(spec.get("available", False))],
        "reviewers_available": [name for name, spec in sorted(reviewers.items()) if isinstance(spec, dict) and bool(spec.get("available", False))],
    }


def build_briefing() -> dict[str, Any]:
    config = load_autonomy_config()
    providers = _provider_summary()
    return {
        "briefing_version": "2026-03-11-runtime-v1",
        "generated_at": utc_now_iso(),
        "main_chain": [
            "event_sourcing",
            "coordinator_heartbeat",
            "ssot_store",
            "task_engine",
            "dashboard",
        ],
        "enabled_task_kinds": ["daily_research", "protocol", "repo"],
        "provider_surface": providers,
        "worker_wakeup": {
            "model": "stateless_cli_worker",
            "sequence": ["role_init_packet", "role_init_ack", "task_packet", "structured_reply"],
            "transport": "fixed-template minimal event packet",
            "notes": [
                "Executor and Reviewer are replaceable CLI workers with no long-term memory",
                "Coordinator must not rely on chat history; it must send fixed-format job packets",
            ],
        },
        "telegram_management": {
            "entrypoint": "scripts/telegram_listener.py",
            "commands": TELEGRAM_COMMANDS,
        },
        "autonomy": {
            "auto_enabled": bool(config.get("auto_enabled", True)),
            "morning_window": str(config.get("morning_window", "")),
            "afternoon_window": str(config.get("afternoon_window", "")),
            "effective_windows": effective_windows(config),
        },
        "research_publish_target": {
            "repo_path": str(resolve_research_publish_repo_path() or "").strip(),
            "work_branch": str(resolve_research_publish_branch() or "main").strip() or "main",
            "blog_repo_path": str(resolve_research_blog_repo_path() or "").strip(),
            "blog_work_branch": str(resolve_research_blog_branch() or "main").strip() or "main",
            "blog_science_dir": str(resolve_research_blog_science_dir() or "content/AGNResearch").strip() or "content/AGNResearch",
        },
        "dependency_policy": {
            "trusted_installs_allowed": True,
            "unknown_sources_forbidden": True,
            "default_sources": [
                "https://download.pytorch.org/whl/cpu",
                "https://pypi.org/simple",
            ],
            "log_every_attempt": True,
        },
        "surface_priority": {
            "primary": PRIMARY_SURFACES,
            "secondary": SECONDARY_SURFACES,
            "legacy": LEGACY_SURFACES,
        },
        "version_change_summary": RELEASE_SUMMARY,
    }


def _briefing_hash(briefing: dict[str, Any]) -> str:
    normalized = dict(briefing)
    normalized.pop("generated_at", None)
    rendered = json.dumps(normalized, ensure_ascii=True, sort_keys=True)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _render_briefing_markdown(briefing: dict[str, Any], change_event: dict[str, Any] | None = None) -> str:
    providers = briefing.get("provider_surface", {}) if isinstance(briefing.get("provider_surface"), dict) else {}
    autonomy = briefing.get("autonomy", {}) if isinstance(briefing.get("autonomy"), dict) else {}
    telegram = briefing.get("telegram_management", {}) if isinstance(briefing.get("telegram_management"), dict) else {}
    surfaces = briefing.get("surface_priority", {}) if isinstance(briefing.get("surface_priority"), dict) else {}
    dependency_policy = briefing.get("dependency_policy", {}) if isinstance(briefing.get("dependency_policy"), dict) else {}
    lines = [
        "# Coordinator Runtime Briefing",
        "",
        f"- generated_at: `{briefing.get('generated_at', '')}`",
        f"- main_chain: `{', '.join(briefing.get('main_chain', []))}`",
        f"- enabled_task_kinds: `{', '.join(briefing.get('enabled_task_kinds', []))}`",
        f"- executor_providers_available: `{', '.join(providers.get('executors_available', [])) or 'n/a'}`",
        f"- reviewer_providers_available: `{', '.join(providers.get('reviewers_available', [])) or 'n/a'}`",
        f"- worker_wakeup: `{', '.join((briefing.get('worker_wakeup') or {}).get('sequence', []))}`",
        f"- telegram_commands: `{', '.join(telegram.get('commands', []))}`",
        f"- auto_enabled: `{bool(autonomy.get('auto_enabled', False))}`",
        f"- morning_window: `{autonomy.get('morning_window', '')}`",
        f"- afternoon_window: `{autonomy.get('afternoon_window', '')}`",
        f"- trusted_dependency_installs_allowed: `{bool(dependency_policy.get('trusted_installs_allowed', False))}`",
        f"- trusted_dependency_sources: `{', '.join(dependency_policy.get('default_sources', []))}`",
        f"- primary_surfaces: `{', '.join(surfaces.get('primary', []))}`",
        f"- secondary_surfaces: `{', '.join(surfaces.get('secondary', []))}`",
        f"- legacy_surfaces: `{', '.join(surfaces.get('legacy', []))}`",
        "",
        "## Current Change Summary",
    ]
    for item in briefing.get("version_change_summary", []):
        lines.append(f"- {item}")
    if isinstance(change_event, dict) and change_event:
        lines.extend(
            [
                "",
                "## Latest Change Event",
                f"- change_id: `{change_event.get('change_id', '')}`",
                f"- changed_at: `{change_event.get('changed_at', '')}`",
                f"- impact_scope: `{', '.join(change_event.get('impact_scope', []))}`",
                f"- requires_refresh: `{bool(change_event.get('requires_refresh', False))}`",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def publish_runtime_surface(
    *,
    reason: str,
    impact_scope: list[str] | None = None,
    affects_coordinator_duties: bool = True,
    affects_worker_init: bool = True,
    affects_telegram_management: bool = True,
    requires_refresh: bool = True,
    force_change_event: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    briefing = build_briefing()
    briefing_hash = _briefing_hash(briefing)
    state = _load_json_or_default(RUNTIME_STATE_PATH, {"last_briefing_hash": "", "last_change_id": ""})
    current_change = _load_json_or_default(CHANGE_EVENT_PATH, {})
    change_needed = force_change_event or state.get("last_briefing_hash") != briefing_hash or not current_change
    if change_needed:
        change_event = {
            "change_id": f"chg-{uuid4().hex[:10]}",
            "changed_at": utc_now_iso(),
            "reason": str(reason or "runtime_surface_refresh").strip() or "runtime_surface_refresh",
            "impact_scope": impact_scope or ["coordinator", "worker_init", "telegram_management", "autonomy"],
            "affects_coordinator_duties": bool(affects_coordinator_duties),
            "affects_worker_init": bool(affects_worker_init),
            "affects_telegram_management": bool(affects_telegram_management),
            "requires_refresh": bool(requires_refresh),
            "briefing_hash": briefing_hash,
            "summary": RELEASE_SUMMARY,
        }
        with CHANGE_EVENT_HISTORY_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(change_event, ensure_ascii=True, sort_keys=True) + "\n")
        _atomic_write_json(CHANGE_EVENT_PATH, change_event)
        state["last_change_id"] = change_event["change_id"]
        append_audit(
            action="network_change_published",
            task_id=None,
            route="/agn/network_runtime",
            status=200,
            change_id=change_event["change_id"],
            reason=change_event["reason"],
            requires_refresh=change_event["requires_refresh"],
        )
    else:
        change_event = current_change
    state["last_briefing_hash"] = briefing_hash
    _atomic_write_json(RUNTIME_STATE_PATH, state)
    _atomic_write_json(BRIEFING_JSON_PATH, briefing)
    _write_text(BRIEFING_MD_PATH, _render_briefing_markdown(briefing, change_event))
    _write_text(REFRESH_MESSAGE_PATH, render_refresh_message())
    _write_text(FIRST_TEST_MESSAGE_PATH, render_first_research_test_message())
    return briefing, change_event


def acknowledge_coordinator_refresh(*, actor: str, refresh_mode: str = "startup") -> dict[str, Any]:
    briefing, change_event = publish_runtime_surface(reason="coordinator_refresh_check")
    ack = {
        "ack_id": f"refresh-{uuid4().hex[:10]}",
        "refreshed_at": utc_now_iso(),
        "actor": str(actor or "coordinator").strip() or "coordinator",
        "refresh_mode": str(refresh_mode or "startup").strip() or "startup",
        "change_id": str(change_event.get("change_id", "")).strip(),
        "briefing_hash": _briefing_hash(briefing),
        "confirmed_main_chain": briefing.get("main_chain", []),
        "confirmed_worker_wakeup": (briefing.get("worker_wakeup") or {}).get("sequence", []),
        "confirmed_telegram_commands": (briefing.get("telegram_management") or {}).get("commands", []),
        "integrity_contract": [
            "truthfulness_first",
            "failure_is_valid",
            "no_fabrication",
        ],
        "duty_delta": [
            "treat Executor and Reviewer as stateless CLI workers",
            "use Telegram as the admin management entrypoint",
            "respect the configured autonomy windows and auto on/off state",
            "do not ask Admin for runtime research decisions outside explicit hold reasons",
            "continue until publish receipt and Telegram delivery receipt exist",
            "treat honesty as mandatory: failure is valid, fabrication is not",
            "allow trusted dependency installs from official sources and log every attempt",
        ],
    }
    _atomic_write_json(DUTY_REFRESH_PATH, ack)
    append_audit(
        action="coordinator_duty_refreshed",
        task_id=None,
        route="/agn/network_runtime",
        status=200,
        actor=ack["actor"],
        change_id=ack["change_id"],
        refresh_mode=ack["refresh_mode"],
    )
    return ack


def render_help_text() -> str:
    config = load_autonomy_config()
    return (
        "[AGN] commands\n"
        "Plain dialogue is not auto-dispatched; use the commands below or an explicit JSON/task envelope.\n"
        "/agn help : show current Telegram admin commands\n"
        "/research start : start manual research intake\n"
        "  minimal: /research start minimal\n"
        "  guided: /research start then reply with:\n"
        "    Research Question: ...\n"
        "    Hypothesis: ...\n"
        "/research status : show current task phase, round, recent event, and trace entry\n"
        "/research pause : pause the active research task\n"
        "/research fallback : force safe fallback topic on the active task\n"
        "/research mark-exception : mark the active task as anomalous\n"
        "/research windows : show morning/afternoon windows and auto state\n"
        "/research set-morning HH:MM : set the survey window\n"
        "/research set-afternoon HH:MM : set the autonomy deadline window\n"
        "/research auto on : enable morning brief + afternoon autonomy\n"
        "/research auto off : disable automatic daily research\n"
        f"auto_enabled={bool(config.get('auto_enabled', False))}\n"
        f"morning_window={str(config.get('morning_window', ''))}\n"
        f"afternoon_window={str(config.get('afternoon_window', ''))}\n"
        f"briefing={BRIEFING_MD_PATH}"
    )


def render_refresh_message() -> str:
    return (
        "Coordinator duty refresh required.\n"
        f"1. Read {BRIEFING_MD_PATH}\n"
        f"2. Read {CHANGE_EVENT_PATH}\n"
        "3. Reply with one short confirmation containing:\n"
        "   - current main chain\n"
        "   - worker wake-up sequence\n"
        "   - Telegram management surface\n"
        "   - your changed duty\n"
        "   - trusted dependency install rule\n"
        "4. After that, resume normal coordination."
    )


def render_first_research_test_message() -> str:
    return (
        "Run one first daily research test under the current AGN protocol.\n"
        "Constraints:\n"
        "- workers are stateless CLI workers\n"
        "- use fixed-template packets only\n"
        "- keep to the configured autonomy windows and Telegram admin surface\n"
        "- if execution needs a missing dependency, install only from trusted official sources and log the attempt\n"
        "- do not ask Admin for runtime research decisions after the task starts unless an explicit hold reason applies\n"
        "Procedure:\n"
        "1. Confirm duty refresh is complete.\n"
        "2. Start one daily_research unit.\n"
        "3. Preserve raw messages, proposal rounds, and any degradation path.\n"
        "4. Archive the unit even if it ends as a failure note.\n"
        "5. Report final archive_ref and review verdict."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish and refresh AGN runtime briefing for Coordinator")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_publish = sub.add_parser("publish")
    p_publish.add_argument("--reason", default="manual_publish")
    p_publish.add_argument("--force-change-event", action="store_true")

    p_refresh = sub.add_parser("refresh")
    p_refresh.add_argument("--actor", default="coordinator")
    p_refresh.add_argument("--mode", default="manual")

    sub.add_parser("help-text")

    args = parser.parse_args()
    if args.cmd == "publish":
        briefing, change_event = publish_runtime_surface(
            reason=str(args.reason or "manual_publish").strip() or "manual_publish",
            force_change_event=bool(args.force_change_event),
        )
        print(json.dumps({"ok": True, "briefing": str(BRIEFING_JSON_PATH), "change_id": change_event.get("change_id", ""), "briefing_hash": _briefing_hash(briefing)}, ensure_ascii=True))
        return 0
    if args.cmd == "refresh":
        ack = acknowledge_coordinator_refresh(actor=str(args.actor or "coordinator").strip(), refresh_mode=str(args.mode or "manual").strip())
        print(json.dumps(ack, ensure_ascii=True))
        return 0
    print(render_help_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
