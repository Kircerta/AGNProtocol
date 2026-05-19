"""AGN current-host fact surface.

This is the real package implementation for AGN's single active host-context
module. The legacy script remains as a compatibility shim.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from agn.core.admin_control import atomic_write_json, read_models_dir, repo_root

try:
    from agn_host_state_probe import HOST_STATE_LOCAL_FILENAME, collect_host_state, write_host_state
except ImportError:  # pragma: no cover
    from agn_host_state_probe import HOST_STATE_LOCAL_FILENAME, collect_host_state, write_host_state


PACKAGE_PATH = "agn.runtime.host_info"
LEGACY_SCRIPT_SHIM = "scripts/agn_host_info.py"


def host_info_md_path() -> Path:
    return repo_root() / "HOST_INFO.md"


def host_info_json_path() -> Path:
    return read_models_dir() / "host_info.json"


def _legacy_paused_read_models() -> tuple[Path, ...]:
    return (
        read_models_dir() / "federated_hosts.json",
        read_models_dir() / "host_state_heartbeat.json",
    )


def _legacy_paused_runtime_dirs() -> tuple[Path, ...]:
    return (repo_root() / "runtime" / "admin_control" / "host_state_heartbeat",)


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _host_state_path() -> Path:
    override = str(os.getenv("AGN_HOST_STATE_PATH", "")).strip()
    return Path(override).expanduser().resolve() if override else (read_models_dir() / HOST_STATE_LOCAL_FILENAME)


def _normalize_available(items: Any) -> tuple[list[str], list[dict[str, str]]]:
    available: list[str] = []
    unavailable: list[dict[str, str]] = []
    if not isinstance(items, list):
        return available, unavailable
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        if bool(item.get("available")):
            available.append(name)
        else:
            unavailable.append(
                {
                    "name": name,
                    "reason": str(item.get("reason", "")).strip() or "unavailable",
                }
            )
    return sorted(set(available)), unavailable


def _freshness_status(payload: dict[str, Any]) -> tuple[str, str]:
    heartbeat = payload.get("heartbeat", {}) if isinstance(payload.get("heartbeat"), dict) else {}
    fresh_until = str(heartbeat.get("fresh_until", "")).strip()
    if not fresh_until:
        return "unknown", "No fresh_until value is recorded for this host state."
    try:
        fresh_until_dt = datetime.fromisoformat(fresh_until.replace("Z", "+00:00"))
    except ValueError:
        return "unknown", "The host state freshness timestamp is invalid."
    if fresh_until_dt.astimezone(timezone.utc) >= datetime.now(tz=timezone.utc):
        return "fresh", "Current host facts are fresh enough for local task-start decisions."
    return "stale", "Current host facts are stale; refresh local host info before trusting it."


def _infer_task_requirements(task_summary: str) -> list[dict[str, str]]:
    text = str(task_summary or "").lower()
    requirements: list[dict[str, str]] = []
    if any(token in text for token in ("chrome", "twitter", "x.com", "browser", "web")):
        requirements.append({"category": "tools", "name": "google_chrome", "reason": "Task mentions browser work or live web inspection."})
    if any(token in text for token in ("obsidian", "vault", "daily note", "meeting note")):
        requirements.append({"category": "tools", "name": "obsidian", "reason": "Task targets the configured Obsidian vault."})
    if any(token in text for token in ("ghostty", "terminal window")):
        requirements.append({"category": "tools", "name": "ghostty", "reason": "Task mentions Ghostty or terminal object control."})
    if any(token in text for token in ("browser-use", "browser automation")):
        requirements.append({"category": "wrappers", "name": "agn_browser_use_wrapper", "reason": "Task expects AGN's controlled browser wrapper."})
    if any(token in text for token in ("promptfoo", "eval", "evaluation", "red-team")):
        requirements.append({"category": "wrappers", "name": "agn_promptfoo_wrapper", "reason": "Task expects AGN's controlled evaluation wrapper."})
    if any(token in text for token in ("hindsight", "memory recall")):
        requirements.append({"category": "wrappers", "name": "agn_hindsight_wrapper", "reason": "Task expects AGN's controlled memory wrapper."})
    if any(token in text for token in ("gui-agent", "desktop adapter", "structured desktop")):
        requirements.append({"category": "tools", "name": "gui_agent", "reason": "Task explicitly wants structured desktop control."})
    if any(token in text for token in ("qwen", "local model")):
        requirements.append({"category": "local_models", "name": "qwen_local_model", "reason": "Task explicitly wants the local Qwen model."})
    if "vertex" in text:
        requirements.append({"category": "providers", "name": "vertex_local", "reason": "Task explicitly wants the local Vertex route."})
    return requirements


def _available_names(payload: dict[str, Any], category: str) -> set[str]:
    runtime = payload.get("runtime_facts", {}) if isinstance(payload.get("runtime_facts"), dict) else {}
    availability = runtime.get("availability", {}) if isinstance(runtime.get("availability"), dict) else {}
    items = availability.get(category, [])
    available, _ = _normalize_available(items)
    return {item.strip().lower() for item in available}


def build_host_info(*, task_summary: str = "", refresh: bool = False) -> dict[str, Any]:
    payload = _load_json(_host_state_path(), {})
    collected_live = False
    if refresh or not isinstance(payload, dict) or not payload:
        payload = collect_host_state()
        write_host_state(payload)
        collected_live = True

    identity = payload.get("host_identity", {}) if isinstance(payload.get("host_identity"), dict) else {}
    static_facts = payload.get("static_facts", {}) if isinstance(payload.get("static_facts"), dict) else {}
    runtime_facts = payload.get("runtime_facts", {}) if isinstance(payload.get("runtime_facts"), dict) else {}
    resources = static_facts.get("resources", {}) if isinstance(static_facts.get("resources"), dict) else {}
    device = static_facts.get("device", {}) if isinstance(static_facts.get("device"), dict) else {}
    path_scope = static_facts.get("path_scope", {}) if isinstance(static_facts.get("path_scope"), dict) else {}
    availability = runtime_facts.get("availability", {}) if isinstance(runtime_facts.get("availability"), dict) else {}
    freshness_status, freshness_summary = _freshness_status(payload)

    providers_available, providers_unavailable = _normalize_available(availability.get("providers", []))
    local_models_available, local_models_unavailable = _normalize_available(availability.get("local_models", []))
    tools_available, tools_unavailable = _normalize_available(availability.get("tools", []))
    wrappers_available, wrappers_unavailable = _normalize_available(availability.get("wrappers", []))

    requirements = _infer_task_requirements(task_summary)
    missing_requirements: list[dict[str, str]] = []
    category_sets = {
        "providers": _available_names(payload, "providers"),
        "local_models": _available_names(payload, "local_models"),
        "tools": _available_names(payload, "tools"),
        "wrappers": _available_names(payload, "wrappers"),
    }
    for requirement in requirements:
        category = str(requirement.get("category", "")).strip()
        name = str(requirement.get("name", "")).strip().lower()
        if not category or not name:
            continue
        if name not in category_sets.get(category, set()):
            missing_requirements.append(requirement)

    if freshness_status == "fresh" and not missing_requirements:
        task_status = "ready"
        task_summary_line = "Current host is ready for this task based on fresh local facts."
    elif missing_requirements:
        task_status = "attention"
        task_summary_line = "Current host is missing one or more task-specific capabilities; inspect the missing requirements before continuing."
    else:
        task_status = "attention"
        task_summary_line = freshness_summary

    missing_or_unknown: list[str] = []
    if not path_scope.get("obsidian_vault"):
        missing_or_unknown.append("obsidian_vault_missing_or_unreadable")
    if freshness_status != "fresh":
        missing_or_unknown.append(f"host_state_{freshness_status}")
    if not providers_available:
        missing_or_unknown.append("no_live_provider_marked_available")

    return {
        "schema_version": "agn.host_info.v1",
        "generated_at": utc_now_iso(),
        "package_path": PACKAGE_PATH,
        "legacy_script_shim": LEGACY_SCRIPT_SHIM,
        "collected_live": collected_live,
        "source": {
            "host_state_path": str(_host_state_path()),
            "host_info_json_path": str(host_info_json_path()),
            "host_info_markdown_path": str(host_info_md_path()),
        },
        "host_identity": {
            "host_id": str(identity.get("host_id", "")).strip(),
            "instance_id": str(identity.get("instance_id", "")).strip(),
            "display_name": str(identity.get("display_name", "")).strip(),
            "host_class": str(identity.get("host_class", "")).strip(),
            "environment": str(identity.get("environment", "")).strip(),
            "role_hint": str(identity.get("role_hint", "")).strip(),
        },
        "device": {
            "hostname": str(device.get("hostname", "")).strip(),
            "os_family": str(device.get("os_family", "")).strip(),
            "os_version": str(device.get("os_version", "")).strip(),
            "architecture": str(device.get("architecture", "")).strip(),
            "device_model": str(device.get("device_model", "")).strip(),
        },
        "resources": {
            "cpu_logical_cores": resources.get("cpu_logical_cores"),
            "memory_total_gb": resources.get("memory_total_gb"),
            "power_profile": resources.get("power_profile", {}),
            "storage_roots": resources.get("storage_roots", []),
            "resource_state": runtime_facts.get("resource_state", {}),
        },
        "dependencies": {
            "providers": {"available": providers_available, "unavailable": providers_unavailable},
            "local_models": {"available": local_models_available, "unavailable": local_models_unavailable},
            "tools": {"available": tools_available, "unavailable": tools_unavailable},
            "wrappers": {"available": wrappers_available, "unavailable": wrappers_unavailable},
        },
        "paths": {
            "repo_root": str(path_scope.get("repo_root", "")).strip(),
            "codex_home": str(path_scope.get("codex_home", "")).strip(),
            "obsidian_vault": str(path_scope.get("obsidian_vault", "")).strip(),
            "local_model_roots": path_scope.get("local_model_roots", []),
        },
        "freshness": {
            "status": freshness_status,
            "summary": freshness_summary,
            "observed_at": str(payload.get("heartbeat", {}).get("observed_at", "")).strip() if isinstance(payload.get("heartbeat"), dict) else "",
            "fresh_until": str(payload.get("heartbeat", {}).get("fresh_until", "")).strip() if isinstance(payload.get("heartbeat"), dict) else "",
        },
        "task_readiness": {
            "task_summary": str(task_summary).strip(),
            "status": task_status,
            "summary": task_summary_line,
            "required_capabilities": requirements,
            "missing_capabilities": missing_requirements,
        },
        "missing_or_unknown": missing_or_unknown,
        "operator_guidance": [
            "Treat this file as the single active host-context surface for AGN.",
            "The operator chooses the active machine before AGN work begins; this file only describes the current host.",
            "If important host information is missing here, inspect the local environment.",
            "Do not infer multi-host scheduling or remote execution from this file.",
        ],
    }


def render_host_info_markdown(payload: dict[str, Any]) -> str:
    host_identity = payload.get("host_identity", {})
    device = payload.get("device", {})
    resources = payload.get("resources", {})
    dependencies = payload.get("dependencies", {})
    paths = payload.get("paths", {})
    freshness = payload.get("freshness", {})
    task_readiness = payload.get("task_readiness", {})
    lines = [
        "# HOST_INFO",
        "",
        "This is AGN's single active host-context surface.",
        "If information is missing here, inspect the local environment.",
        "",
        "## Identity",
        f"- Host ID: `{host_identity.get('host_id', '')}`",
        f"- Display name: `{host_identity.get('display_name', '')}`",
        f"- Host class: `{host_identity.get('host_class', '')}`",
        f"- Environment: `{host_identity.get('environment', '')}`",
        f"- Role hint: `{host_identity.get('role_hint', '')}`",
        "",
        "## Device",
        f"- Hostname: `{device.get('hostname', '')}`",
        f"- OS: `{device.get('os_family', '')} {device.get('os_version', '')}`".rstrip(),
        f"- Architecture: `{device.get('architecture', '')}`",
        f"- Model: `{device.get('device_model', '')}`",
        "",
        "## Resources",
        f"- CPU logical cores: `{resources.get('cpu_logical_cores', '')}`",
        f"- Memory total GB: `{resources.get('memory_total_gb', '')}`",
        f"- Freshness: `{freshness.get('status', '')}`",
        f"- Freshness summary: {freshness.get('summary', '')}",
        "",
        "## Dependencies",
        f"- Providers available: {', '.join(dependencies.get('providers', {}).get('available', [])) or 'none'}",
        f"- Local models available: {', '.join(dependencies.get('local_models', {}).get('available', [])) or 'none'}",
        f"- Tools available: {', '.join(dependencies.get('tools', {}).get('available', [])) or 'none'}",
        f"- Wrappers available: {', '.join(dependencies.get('wrappers', {}).get('available', [])) or 'none'}",
        "",
        "## Paths",
        f"- Repo root: `{paths.get('repo_root', '')}`",
        f"- Codex home: `{paths.get('codex_home', '')}`",
        f"- Obsidian vault: `{paths.get('obsidian_vault', '')}`",
        "",
        "## Task Readiness",
        f"- Status: `{task_readiness.get('status', '')}`",
        f"- Summary: {task_readiness.get('summary', '')}",
    ]
    missing = task_readiness.get("missing_capabilities", [])
    if isinstance(missing, list) and missing:
        lines.append("- Missing capabilities:")
        for item in missing:
            if not isinstance(item, dict):
                continue
            lines.append(f"  - `{item.get('category', '')}:{item.get('name', '')}`: {item.get('reason', '')}")
    unknown = payload.get("missing_or_unknown", [])
    lines.append("")
    lines.append("## Missing Or Unknown")
    if isinstance(unknown, list) and unknown:
        for item in unknown:
            lines.append(f"- `{item}`")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("## Guidance")
    for item in payload.get("operator_guidance", []):
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def write_host_info(payload: dict[str, Any]) -> dict[str, str]:
    atomic_write_json(host_info_json_path(), payload)
    host_info_md_path().write_text(render_host_info_markdown(payload), encoding="utf-8")
    for path in _legacy_paused_read_models():
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
    for path in _legacy_paused_runtime_dirs():
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass
    return {
        "json": str(host_info_json_path()),
        "markdown": str(host_info_md_path()),
    }


def cmd_refresh(args: argparse.Namespace) -> int:
    payload = build_host_info(task_summary=str(args.task_summary or "").strip(), refresh=True)
    payload["written_paths"] = write_host_info(payload)
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    payload = build_host_info(task_summary=str(args.task_summary or "").strip(), refresh=bool(args.refresh))
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build AGN's single-host hardware and dependency context surface.")
    sub = parser.add_subparsers(dest="command", required=True)
    refresh = sub.add_parser("refresh", help="Collect fresh local host info and write host_info read models.")
    refresh.add_argument("--task-summary", default="")
    refresh.set_defaults(func=cmd_refresh)
    show = sub.add_parser("show", help="Show current host info without forcing a fresh write unless requested.")
    show.add_argument("--task-summary", default="")
    show.add_argument("--refresh", action="store_true")
    show.set_defaults(func=cmd_show)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
