#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from admin_control_common import atomic_write_json, read_models_dir

CATALOG_ENV_VAR = "AGN_TOOL_REALITY_CARDS_PATH"
LOCAL_CATALOG_PATH = ROOT / "config" / "tool_reality_cards.local.json"
EXAMPLE_CATALOG_PATH = ROOT / "config" / "tool_reality_cards.example.json"
DEFAULT_HOST_STATE_PATH = read_models_dir() / "federated_host_state.local.json"
READ_MODEL_PATH = read_models_dir() / "tool_reality_cards.json"


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def parse_iso8601(raw: str) -> datetime | None:
    value = str(raw or "").strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _load_json(path: Path, default: Any) -> Any:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return payload


def _host_state_path() -> Path:
    override = str(os.getenv("AGN_HOST_STATE_PATH", "")).strip()
    return Path(override).expanduser().resolve() if override else DEFAULT_HOST_STATE_PATH


def _catalog_path() -> Path:
    override = str(os.getenv(CATALOG_ENV_VAR, "")).strip()
    if override:
        return Path(override).expanduser().resolve()
    if LOCAL_CATALOG_PATH.exists():
        return LOCAL_CATALOG_PATH
    return EXAMPLE_CATALOG_PATH


def _expand_path(raw: str) -> Path:
    return Path(raw).expanduser().resolve()


def _normalize_many(values: list[str] | tuple[str, ...] | None) -> list[str]:
    return sorted({str(value or "").strip().lower() for value in (values or []) if str(value or "").strip()})


def _freshness_status(host_state: dict[str, Any], *, now: datetime) -> str:
    heartbeat = host_state.get("heartbeat", {})
    if not isinstance(heartbeat, dict):
        return "unknown"
    fresh_until = parse_iso8601(str(heartbeat.get("fresh_until", "")).strip())
    if fresh_until is None:
        return "unknown"
    return "fresh" if fresh_until.astimezone(timezone.utc) >= now else "stale"


def _availability_items(host_state: dict[str, Any], category: str) -> list[dict[str, Any]]:
    runtime = host_state.get("runtime_facts", {})
    if not isinstance(runtime, dict):
        return []
    availability = runtime.get("availability", {})
    if not isinstance(availability, dict):
        return []
    items = availability.get(category, [])
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _runtime_match(host_state: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any] | None:
    category = str(binding.get("category", "")).strip()
    names = _normalize_many(binding.get("names", []))
    if not category or not names:
        return None
    for item in _availability_items(host_state, category):
        name = str(item.get("name", "")).strip().lower()
        if name in names:
            return {
                "configured": bool(item.get("configured")),
                "available": bool(item.get("available")),
                "reason": str(item.get("reason", "")).strip(),
                "source": f"runtime_facts.availability.{category}.{name}",
            }
    return None


def _path_check_status(path_raw: str) -> dict[str, Any]:
    path = _expand_path(path_raw)
    exists = path.exists()
    return {
        "configured": exists,
        "available": exists,
        "reason": "" if exists else f"missing_path:{path}",
        "source": f"path_check:{path}",
    }


def _command_check_status(command: str) -> dict[str, Any]:
    found = shutil.which(command) or ""
    return {
        "configured": bool(found),
        "available": bool(found),
        "reason": "" if found else f"command_not_found:{command}",
        "source": f"command_check:{command}",
    }


def _best_status(host_state: dict[str, Any], card: dict[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for binding in card.get("current_bindings", []):
        if isinstance(binding, dict):
            match = _runtime_match(host_state, binding)
            if match:
                candidates.append(match)
    for path_raw in card.get("path_checks", []):
        candidates.append(_path_check_status(str(path_raw)))
    for command in card.get("command_checks", []):
        candidates.append(_command_check_status(str(command)))
    if not candidates:
        return {
            "configured": None,
            "available": None,
            "reason": "no_runtime_binding",
            "source": "none",
        }
    candidates.sort(key=lambda item: (1 if item.get("available") is True else 0, 1 if item.get("configured") is True else 0), reverse=True)
    return candidates[0]


def _matched_host_notes(card: dict[str, Any], *, host_id: str, host_class: str) -> list[str]:
    notes: list[str] = []
    for item in card.get("host_overrides", []):
        if not isinstance(item, dict):
            continue
        host_ids = _normalize_many(item.get("host_ids", []))
        host_classes = _normalize_many(item.get("host_classes", []))
        note = str(item.get("note", "")).strip()
        if not note:
            continue
        if (host_ids and host_id in host_ids) or (host_classes and host_class in host_classes):
            notes.append(note)
    return notes


def _assessment(status: dict[str, Any], freshness_status: str) -> tuple[str, str]:
    available = status.get("available")
    configured = status.get("configured")
    reason = str(status.get("reason", "")).strip()
    if available is True and freshness_status == "fresh":
        return "suitable", "Live host state and checks indicate the tool is currently usable on this host."
    if available is True:
        return "limited", "The tool appears present, but the current host state is stale or incomplete, so suitability is not fully fresh."
    if available is False and configured is False:
        return "unavailable", reason or "The tool is not configured on this host."
    if available is False:
        return "unavailable", reason or "The tool is configured but currently unavailable on this host."
    return "unknown", reason or "No live binding resolved for this tool."


def _current_host_identity(host_state: dict[str, Any]) -> dict[str, str]:
    identity = host_state.get("host_identity", {})
    if not isinstance(identity, dict):
        identity = {}
    return {
        "host_id": str(identity.get("host_id", "")).strip().lower(),
        "host_class": str(identity.get("host_class", "")).strip().lower(),
        "display_name": str(identity.get("display_name", "")).strip(),
    }


def _catalog_cards() -> list[dict[str, Any]]:
    payload = _load_json(_catalog_path(), {})
    if not isinstance(payload, dict):
        return []
    cards = payload.get("cards", [])
    return [card for card in cards if isinstance(card, dict)]


def resolve_tool_reality_cards(*, tool_ids: list[str] | None = None, host_state_path: Path | None = None, federated_hosts_path: Path | None = None) -> dict[str, Any]:
    _ = federated_hosts_path
    host_state = _load_json(host_state_path or _host_state_path(), {})
    now = utc_now()
    identity = _current_host_identity(host_state if isinstance(host_state, dict) else {})
    freshness_status = _freshness_status(host_state if isinstance(host_state, dict) else {}, now=now)
    requested = _normalize_many(tool_ids)
    cards_out: list[dict[str, Any]] = []
    for card in _catalog_cards():
        tool_id = str(card.get("tool_id", "")).strip().lower()
        if requested and tool_id not in requested:
            continue
        current_status = _best_status(host_state if isinstance(host_state, dict) else {}, card)
        assessment, explanation = _assessment(current_status, freshness_status)
        cards_out.append(
            {
                "tool_identity": {
                    "tool_id": tool_id,
                    "display_name": str(card.get("display_name", "")).strip() or tool_id,
                    "category": str(card.get("category", "")).strip() or "other",
                },
                "purpose": str(card.get("purpose", "")).strip(),
                "current_host_availability": {
                    "host_id": identity["host_id"],
                    "host_class": identity["host_class"],
                    "display_name": identity["display_name"],
                    "freshness_status": freshness_status,
                    "configured": current_status.get("configured"),
                    "available": current_status.get("available"),
                    "status": assessment,
                    "explanation": explanation,
                    "reason": str(current_status.get("reason", "")).strip(),
                    "source": str(current_status.get("source", "")).strip(),
                },
                "prerequisites": [str(item).strip() for item in card.get("prerequisites", []) if str(item).strip()],
                "authority_boundary": str(card.get("authority_boundary", "")).strip(),
                "session_limitations": [str(item).strip() for item in card.get("session_limitations", []) if str(item).strip()],
                "known_failure_modes": [str(item).strip() for item in card.get("known_failure_modes", []) if str(item).strip()],
                "host_specific_notes": _matched_host_notes(card, host_id=identity["host_id"], host_class=identity["host_class"]),
                "source_refs": [str(item).strip() for item in card.get("source_refs", []) if str(item).strip()],
            }
        )
    return {
        "schema_version": "agn.tool_reality_cards.v1",
        "generated_at": utc_now_iso(),
        "current_host": {
            "host_id": identity["host_id"],
            "host_class": identity["host_class"],
            "display_name": identity["display_name"],
            "host_state_path": str(host_state_path or _host_state_path()),
        },
        "cards": cards_out,
        "summary": {
            "count": len(cards_out),
            "suitable_now": sorted(card["tool_identity"]["tool_id"] for card in cards_out if card["current_host_availability"]["status"] == "suitable"),
            "limited_now": sorted(card["tool_identity"]["tool_id"] for card in cards_out if card["current_host_availability"]["status"] == "limited"),
            "unavailable_now": sorted(card["tool_identity"]["tool_id"] for card in cards_out if card["current_host_availability"]["status"] == "unavailable"),
        },
    }


def build_tool_reality_summary() -> dict[str, Any]:
    payload = resolve_tool_reality_cards()
    return {
        "count": int(payload.get("summary", {}).get("count", 0)),
        "suitable_now": list(payload.get("summary", {}).get("suitable_now", [])),
        "limited_now": list(payload.get("summary", {}).get("limited_now", [])),
        "unavailable_now": list(payload.get("summary", {}).get("unavailable_now", [])),
    }


def write_tool_reality_read_model(payload: dict[str, Any], *, output_path: Path | None = None) -> Path:
    target = output_path or READ_MODEL_PATH
    atomic_write_json(target, payload)
    return target


def cmd_build(args: argparse.Namespace) -> int:
    payload = resolve_tool_reality_cards(tool_ids=list(args.tool or []))
    if not args.no_write:
        write_tool_reality_read_model(payload, output_path=Path(args.output).expanduser().resolve() if args.output else None)
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    payload = resolve_tool_reality_cards(tool_ids=[str(args.tool).strip()])
    cards = payload.get("cards", [])
    if not cards:
        print(json.dumps({"ok": False, "error": "tool_reality_card_not_found", "tool": args.tool}, ensure_ascii=True, indent=2))
        return 1
    print(json.dumps(cards[0], ensure_ascii=True, indent=2))
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    payload = resolve_tool_reality_cards()
    listing = [
        {
            "tool_id": card["tool_identity"]["tool_id"],
            "display_name": card["tool_identity"]["display_name"],
            "status": card["current_host_availability"]["status"],
        }
        for card in payload.get("cards", [])
    ]
    print(json.dumps({"ok": True, "tools": listing}, ensure_ascii=True, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resolve AGN tool reality cards against the current host only.")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="Build the current host tool reality card read model.")
    build.add_argument("--tool", action="append", default=[])
    build.add_argument("--output", default="")
    build.add_argument("--no-write", action="store_true")
    build.set_defaults(func=cmd_build)
    show = sub.add_parser("show", help="Show one resolved tool reality card.")
    show.add_argument("--tool", required=True)
    show.set_defaults(func=cmd_show)
    listing = sub.add_parser("list", help="List resolved tool reality cards.")
    listing.set_defaults(func=cmd_list)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
