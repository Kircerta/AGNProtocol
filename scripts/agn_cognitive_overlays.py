#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OVERLAYS_DIR = ROOT / "agn2" / "cognitive_overlays"
OPEN_SOURCE_ROOT = ROOT.parent / "OpenSource"


def _overlay_catalog() -> dict[str, dict[str, Any]]:
    return {
        "coding-criticality": {
            "path": OVERLAYS_DIR / "coding-criticality.md",
            "summary": "Critical coding workflow overlay for implementation, debugging, testing, and code review.",
            "when": ["implement", "refactor", "debug", "test", "feature", "review", "bug", "code"],
            "external_refs": [
                OPEN_SOURCE_ROOT / "superpowers" / "README.md",
                OPEN_SOURCE_ROOT / "promptfoo" / "README.md",
            ],
        },
        "agent-eval-and-redteam": {
            "path": OVERLAYS_DIR / "agent-eval-and-redteam.md",
            "summary": "Evaluation and adversarial-testing overlay for agentic systems, MCP, and prompt security.",
            "when": ["eval", "evaluation", "red team", "red-team", "security", "mcp", "trajectory", "agent"],
            "external_refs": [
                OPEN_SOURCE_ROOT / "promptfoo" / "README.md",
            ],
        },
        "academic-writing-critic": {
            "path": OVERLAYS_DIR / "academic-writing-critic.md",
            "summary": "Academic writing rigor overlay for literature review, argument quality, and evidence-bounded prose.",
            "when": ["paper", "essay", "abstract", "literature review", "research writing", "methodology", "discussion", "conclusion"],
            "external_refs": [
                OPEN_SOURCE_ROOT / "chatgpt-prompts-for-academic-writing" / "README.md",
            ],
        },
        "memory-recall-before-action": {
            "path": OVERLAYS_DIR / "memory-recall-before-action.md",
            "summary": "Recall-first overlay for tasks shaped by prior decisions, preferences, and repeated learnings.",
            "when": ["memory", "recall", "preference", "history", "decision", "context", "drift"],
            "external_refs": [
                OPEN_SOURCE_ROOT / "hindsight" / "README.md",
            ],
        },
    }


def list_overlays() -> list[dict[str, Any]]:
    items = []
    for name, info in sorted(_overlay_catalog().items()):
        items.append(
            {
                "name": name,
                "path": str(info["path"]),
                "exists": bool(Path(info["path"]).exists()),
                "summary": info["summary"],
            }
        )
    return items


def show_overlay(name: str) -> dict[str, Any]:
    catalog = _overlay_catalog()
    if name not in catalog:
        raise KeyError(name)
    info = catalog[name]
    return {
        "name": name,
        "path": str(info["path"]),
        "exists": bool(Path(info["path"]).exists()),
        "summary": info["summary"],
        "when": list(info["when"]),
        "external_refs": [str(item) for item in info["external_refs"]],
    }


def recommend_overlays(task_summary: str) -> list[dict[str, Any]]:
    text = str(task_summary or "").lower()
    selected: list[dict[str, Any]] = []
    for name, info in sorted(_overlay_catalog().items()):
        if any(token in text for token in info["when"]):
            selected.append(show_overlay(name))
    if not selected and any(token in text for token in ("implement", "build", "change", "fix")):
        selected.append(show_overlay("coding-criticality"))
    return selected


def _print_list(items: list[dict[str, Any]]) -> None:
    for item in items:
        status = "ready" if item["exists"] else "missing"
        print(f"- {item['name']} [{status}]: {item['summary']}")


def _print_show(item: dict[str, Any]) -> None:
    print(f"{item['name']}: {item['summary']}")
    print(f"path: {item['path']}")
    if item["when"]:
        print("when:")
        for token in item["when"]:
            print(f"- {token}")
    if item["external_refs"]:
        print("external_refs:")
        for ref in item["external_refs"]:
            print(f"- {ref}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect and recommend AGN cognitive overlays.")
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="List overlays")
    list_parser.add_argument("--json", action="store_true")

    show_parser = sub.add_parser("show", help="Show one overlay")
    show_parser.add_argument("name")
    show_parser.add_argument("--json", action="store_true")

    recommend_parser = sub.add_parser("recommend", help="Recommend overlays for a task summary")
    recommend_parser.add_argument("--task-summary", required=True)
    recommend_parser.add_argument("--json", action="store_true")

    args = parser.parse_args()

    if args.command == "list":
        payload = list_overlays()
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            _print_list(payload)
        return 0
    if args.command == "show":
        payload = show_overlay(args.name)
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            _print_show(payload)
        return 0
    payload = recommend_overlays(args.task_summary)
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        _print_list(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
