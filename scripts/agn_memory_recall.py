#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from memory_recorder import query_agent_findings
try:
    from agn_tool_reality_cards import resolve_tool_reality_cards
except ImportError:  # pragma: no cover
    from scripts.agn_tool_reality_cards import resolve_tool_reality_cards

DEFAULT_CATALOG_PATH = ROOT / "memory" / "priors" / "recall_catalog.json"
DEFAULT_LOCAL_HOST_STATE_PATH = ROOT / "runtime" / "admin_control" / "read_models" / "federated_host_state.local.json"
TASK_TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "social_monitoring": ("twitter", "x.com", "social", "following", "feed", "monitor", "news"),
    "browser": ("browser", "chrome", "web", "site", "page", "form", "login"),
    "desktop_gui": ("desktop", "window", "ghostty", "gui", "screenshot", "focus", "click"),
    "coding": ("code", "coding", "implement", "refactor", "fix", "patch", "repo"),
    "evaluation": ("eval", "evaluate", "red-team", "benchmark", "promptfoo", "report"),
    "memory": ("memory", "recall", "history", "re-entry", "context"),
}
NOISY_MATCH_TOKENS = {
    "codex",
    "claude",
    "gemini",
    "deepseek",
    "vertex_local",
    "qwen_local",
    "monitor",
    "coding",
    "general",
    "browser",
    "memory",
    "desktop",
}
TOOL_HINTS = {
    "browser-use": ("browser-use",),
    "gui_agent": ("gui-agent", "gui_agent"),
    "ghostty": ("ghostty",),
    "obsidian": ("obsidian",),
}
PROVIDER_HINTS = {
    "qwen_local": ("qwen", "qwen_local"),
    "vertex_local": ("vertex", "vertex_local"),
    "deepseek": ("deepseek",),
    "claude": ("claude",),
    "gemini": ("gemini",),
    "codex": ("codex",),
}


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json(path: Path, default: Any) -> Any:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return payload


def _catalog_path() -> Path:
    override = str(os.getenv("AGN_MEMORY_RECALL_CATALOG_PATH", "")).strip()
    return Path(override).expanduser().resolve() if override else DEFAULT_CATALOG_PATH


def _host_state_path() -> Path:
    override = str(os.getenv("AGN_HOST_STATE_PATH", "")).strip()
    return Path(override).expanduser().resolve() if override else DEFAULT_LOCAL_HOST_STATE_PATH


def _normalize_many(values: list[str] | tuple[str, ...] | None) -> list[str]:
    normalized: list[str] = []
    for value in values or []:
        item = str(value or "").strip().lower()
        if item and item not in normalized:
            normalized.append(item)
    return normalized


def derive_task_type(task_summary: str, explicit: str = "") -> str:
    chosen = str(explicit or "").strip().lower()
    if chosen:
        return chosen
    summary = str(task_summary or "").strip().lower()
    for task_type, keywords in TASK_TYPE_KEYWORDS.items():
        if any(keyword in summary for keyword in keywords):
            return task_type
    return "general"


def _infer_from_summary(task_summary: str, hints: dict[str, tuple[str, ...]]) -> list[str]:
    lowered = str(task_summary or "").strip().lower()
    matched: list[str] = []
    for canonical, variants in hints.items():
        if any(variant in lowered for variant in variants):
            matched.append(canonical)
    return matched


def current_runtime_context() -> dict[str, Any]:
    payload = _load_json(_host_state_path(), {})
    if not isinstance(payload, dict):
        payload = {}
    host_identity = payload.get("host_identity", {})
    if not isinstance(host_identity, dict):
        host_identity = {}
    availability = payload.get("runtime_facts", {}).get("availability", {}) if isinstance(payload.get("runtime_facts"), dict) else {}
    tools = availability.get("tools", []) if isinstance(availability, dict) else []
    providers = availability.get("providers", []) if isinstance(availability, dict) else []
    local_models = availability.get("local_models", []) if isinstance(availability, dict) else []

    def _items(raw: Any) -> dict[str, list[str]]:
        available: list[str] = []
        unavailable: list[str] = []
        reasons: dict[str, str] = {}
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip().lower()
                if not name:
                    continue
                if bool(item.get("available")):
                    available.append(name)
                else:
                    unavailable.append(name)
                    reason = str(item.get("reason", "")).strip()
                    if reason:
                        reasons[name] = reason
        return {
            "available": sorted(set(available)),
            "unavailable": sorted(set(unavailable)),
            "reasons": reasons,
        }

    return {
        "host_id": str(host_identity.get("host_id", "")).strip().lower(),
        "host_class": str(host_identity.get("host_class", "")).strip().lower(),
        "environment": str(host_identity.get("environment", "")).strip().lower(),
        "tools": _items(tools),
        "providers": _items(providers),
        "local_models": _items(local_models),
        "runtime_source": str(_host_state_path()),
    }


def _load_catalog() -> list[dict[str, Any]]:
    payload = _load_json(_catalog_path(), {})
    if not isinstance(payload, dict):
        return []
    priors = payload.get("priors", [])
    return [item for item in priors if isinstance(item, dict)]


def _match_catalog_prior(entry: dict[str, Any], *, task_type: str, host_id: str, tools: list[str], providers: list[str], task_summary: str) -> tuple[bool, list[str]]:
    applies = entry.get("applies_to", {})
    if not isinstance(applies, dict):
        applies = {}
    matches: list[str] = []
    allowed_task_types = _normalize_many(applies.get("task_types", []))
    allowed_hosts = _normalize_many(applies.get("host_ids", []))
    allowed_tools = _normalize_many(applies.get("tools", []))
    allowed_providers = _normalize_many(applies.get("providers", []))
    keywords = _normalize_many(applies.get("keywords", []))

    has_constraint = any([allowed_task_types, allowed_hosts, allowed_tools, allowed_providers, keywords])
    if allowed_task_types and task_type in allowed_task_types:
        matches.append(f"task_type:{task_type}")
    if allowed_hosts and host_id and host_id in allowed_hosts:
        matches.append(f"host:{host_id}")
    for tool in tools:
        if tool in allowed_tools:
            matches.append(f"tool:{tool}")
    for provider in providers:
        if provider in allowed_providers:
            matches.append(f"provider:{provider}")
    lowered_summary = task_summary.lower()
    for keyword in keywords:
        if keyword in lowered_summary:
            matches.append(f"keyword:{keyword}")
    return ((not has_constraint) or bool(matches)), sorted(set(matches))


def _relation_for_prior(entry: dict[str, Any], runtime_context: dict[str, Any]) -> str:
    applies = entry.get("applies_to", {})
    if not isinstance(applies, dict):
        return "memory_only"
    tool_names = _normalize_many(applies.get("tools", []))
    provider_names = _normalize_many(applies.get("providers", []))
    host_names = _normalize_many(applies.get("host_ids", []))
    runtime_host = str(runtime_context.get("host_id", "")).strip().lower()
    runtime_tools = runtime_context.get("tools", {}) if isinstance(runtime_context.get("tools"), dict) else {}
    runtime_providers = runtime_context.get("providers", {}) if isinstance(runtime_context.get("providers"), dict) else {}
    runtime_models = runtime_context.get("local_models", {}) if isinstance(runtime_context.get("local_models"), dict) else {}
    available_tools = set(_normalize_many(runtime_tools.get("available", [])))
    unavailable_tools = set(_normalize_many(runtime_tools.get("unavailable", [])))
    available_providers = set(_normalize_many(runtime_providers.get("available", []))) | set(_normalize_many(runtime_models.get("available", [])))
    unavailable_providers = set(_normalize_many(runtime_providers.get("unavailable", []))) | set(_normalize_many(runtime_models.get("unavailable", [])))

    if host_names and runtime_host and runtime_host in host_names:
        if any(name in unavailable_tools for name in tool_names) or any(name in unavailable_providers for name in provider_names):
            return "confirmed_by_runtime"
        if any(name in available_tools for name in tool_names) or any(name in available_providers for name in provider_names):
            return "not_confirmed_by_runtime"
        return "memory_only"
    if any(name in unavailable_tools for name in tool_names) or any(name in unavailable_providers for name in provider_names):
        return "confirmed_by_runtime"
    if any(name in available_tools for name in tool_names) or any(name in available_providers for name in provider_names):
        return "not_confirmed_by_runtime"
    return "memory_only"


def _match_append_only_record(record: dict[str, Any], *, task_type: str, host_id: str, tools: list[str], providers: list[str], task_summary: str) -> tuple[int, list[str]]:
    haystack_parts = [
        str(record.get("summary", "")),
        json.dumps(record.get("fact_payload", {}), ensure_ascii=False, sort_keys=True),
        " ".join(str(item) for item in record.get("source_refs", []) if str(item).strip()),
        str(record.get("scope", "")),
    ]
    haystack = " ".join(haystack_parts).lower()
    tokens = set(_normalize_many([task_type, host_id, *tools, *providers]))
    tokens.update(token.lower() for token in re.findall(r"[a-zA-Z0-9_.-]{5,}", task_summary))
    filtered_tokens = {token for token in tokens if token not in NOISY_MATCH_TOKENS}
    tokens = filtered_tokens
    matched = sorted(token for token in tokens if token and token in haystack)
    return len(matched), matched


def _append_only_priors(*, task_type: str, host_id: str, tools: list[str], providers: list[str], task_summary: str) -> list[dict[str, Any]]:
    records = query_agent_findings(scope="agn2/codex", limit=120)
    hits: list[tuple[int, dict[str, Any], list[str]]] = []
    for record in records:
        score, matched = _match_append_only_record(
            record,
            task_type=task_type,
            host_id=host_id,
            tools=tools,
            providers=providers,
            task_summary=task_summary,
        )
        if score >= 2:
            hits.append((score, record, matched))
    hits.sort(key=lambda item: (item[0], str(item[1].get("ts", ""))), reverse=True)
    priors: list[dict[str, Any]] = []
    for score, record, matched in hits[:5]:
        priors.append(
            {
                "source_kind": "append_only_memory",
                "id": str(record.get("record_id", "")).strip() or f"memory-{score}",
                "kind": str(record.get("kind", "fact")).strip() or "fact",
                "summary": str(record.get("summary", "")).strip(),
                "matched_on": [f"memory:{token}" for token in matched],
                "runtime_relation": "memory_only",
                "confidence": str(record.get("confidence", "medium")).strip() or "medium",
                "source_refs": [str(item).strip() for item in record.get("source_refs", []) if str(item).strip()],
                "fact_payload": record.get("fact_payload", {}) if isinstance(record.get("fact_payload"), dict) else {},
            }
        )
    return priors


def query_memory_recall(
    *,
    task_summary: str,
    task_type: str = "",
    host_id: str = "",
    tools: list[str] | None = None,
    providers: list[str] | None = None,
    runtime_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_task_type = derive_task_type(task_summary, task_type)
    normalized_tools = _normalize_many(tools or [])
    normalized_providers = _normalize_many(providers or [])
    runtime = runtime_context if isinstance(runtime_context, dict) else current_runtime_context()
    effective_host_id = str(host_id or runtime.get("host_id", "")).strip().lower()
    if not normalized_tools:
        normalized_tools = _normalize_many(_infer_from_summary(task_summary, TOOL_HINTS))
    if not normalized_tools:
        normalized_tools = _normalize_many(runtime.get("tools", {}).get("available", []))
    if not normalized_providers:
        normalized_providers = _normalize_many(_infer_from_summary(task_summary, PROVIDER_HINTS))
    if not normalized_providers:
        normalized_providers = _normalize_many(runtime.get("providers", {}).get("available", []))

    priors: list[dict[str, Any]] = []
    for entry in _load_catalog():
        matched, matched_on = _match_catalog_prior(
            entry,
            task_type=normalized_task_type,
            host_id=effective_host_id,
            tools=normalized_tools,
            providers=normalized_providers,
            task_summary=task_summary,
        )
        if not matched:
            continue
        priors.append(
            {
                "source_kind": "catalog",
                "id": str(entry.get("id", "")).strip() or "catalog-prior",
                "kind": str(entry.get("kind", "prior")).strip() or "prior",
                "summary": str(entry.get("summary", "")).strip(),
                "matched_on": matched_on,
                "runtime_relation": _relation_for_prior(entry, runtime),
                "confidence": str(entry.get("strength", "medium")).strip() or "medium",
                "source_refs": [str(item).strip() for item in entry.get("source_refs", []) if str(item).strip()],
                "fact_payload": entry.get("fact_payload", {}) if isinstance(entry.get("fact_payload"), dict) else {},
            }
        )

    priors.extend(
        _append_only_priors(
            task_type=normalized_task_type,
            host_id=effective_host_id,
            tools=normalized_tools,
            providers=normalized_providers,
            task_summary=task_summary,
        )
    )
    reality_cards_payload = resolve_tool_reality_cards(tool_ids=sorted(set([*normalized_tools, *normalized_providers])))
    reality_cards = reality_cards_payload.get("cards", []) if isinstance(reality_cards_payload, dict) else []

    advisory_summary = [prior["summary"] for prior in priors[:5] if str(prior.get("summary", "")).strip()]
    return {
        "ok": True,
        "queried_at": utc_now_iso(),
        "query": {
            "task_summary": task_summary,
            "task_type": normalized_task_type,
            "host_id": effective_host_id,
            "tools": normalized_tools,
            "providers": normalized_providers,
        },
        "runtime_context": runtime,
        "priors": priors,
        "tool_reality_cards": reality_cards,
        "advisory_summary": advisory_summary,
        "runtime_priority_notice": "Runtime facts remain authoritative. Memory priors are advisory and may be confirmed or overridden by live host state.",
    }


def cmd_query(args: argparse.Namespace) -> int:
    payload = query_memory_recall(
        task_summary=str(args.task_summary),
        task_type=str(args.task_type or ""),
        host_id=str(args.host_id or ""),
        tools=list(args.tool or []),
        providers=list(args.provider or []),
    )
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Recall AGN memory priors for a task before execution.")
    sub = parser.add_subparsers(dest="command", required=True)
    query = sub.add_parser("query", help="Query memory priors by task, host, tool, and provider context.")
    query.add_argument("--task-summary", required=True)
    query.add_argument("--task-type", default="")
    query.add_argument("--host-id", default="")
    query.add_argument("--tool", action="append", default=[])
    query.add_argument("--provider", action="append", default=[])
    query.set_defaults(func=cmd_query)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
