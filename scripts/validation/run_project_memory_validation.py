#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "reports" / "project_memory_validation.json"

REQUIRED_FILES = {
    "README.md": ["## Requirements", "## Install", "## Configure", "## Core Commands"],
    "PROJECT_BRIEF.md": ["## Core Surfaces", "## Runtime Boundaries", "## State Directories"],
    "RUNBOOK.md": ["## Setup", "## Health Checks", "## Lifecycle", "## Tests"],
    "KNOWN_ISSUES.md": ["## Local Runtime State", "## Provider Availability", "## Test Suite"],
}

MAX_LINES = {
    "README.md": 120,
    "PROJECT_BRIEF.md": 80,
    "RUNBOOK.md": 160,
    "KNOWN_ISSUES.md": 100,
}


def _project_roots() -> dict[str, Path]:
    projects: dict[str, Path] = {"AgenticNetwork": ROOT}
    sibling_machine_deck = ROOT.parent / "MachineDeck"
    legacy_machine_deck = Path("<machine-deck-root>")
    if sibling_machine_deck.exists():
        projects["MachineDeck"] = sibling_machine_deck
    elif legacy_machine_deck.exists():
        projects["MachineDeck"] = legacy_machine_deck
    return projects


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _line_count(text: str) -> int:
    return len([line for line in text.splitlines() if line.strip()])


def _extract_section(text: str, heading: str) -> list[str]:
    lines = text.splitlines()
    capture = False
    collected: list[str] = []
    for line in lines:
        if line.strip() == heading:
            capture = True
            continue
        if capture and line.startswith("## "):
            break
        if capture and line.strip():
            collected.append(line.strip())
    return collected


def _project_report(project_name: str, base: Path) -> dict[str, Any]:
    report: dict[str, Any] = {
        "project": project_name,
        "path": str(base),
        "ok": True,
        "files": {},
        "reentry_digest": {},
        "errors": [],
    }
    for filename, markers in REQUIRED_FILES.items():
        path = base / filename
        info: dict[str, Any] = {"exists": path.exists(), "markers_ok": True, "line_count_ok": True, "line_count": 0}
        if not path.exists():
            report["ok"] = False
            report["errors"].append(f"missing_file:{filename}")
            info["markers_ok"] = False
            info["line_count_ok"] = False
            report["files"][filename] = info
            continue
        text = _read(path)
        info["line_count"] = _line_count(text)
        if info["line_count"] > MAX_LINES[filename]:
            info["line_count_ok"] = False
            report["ok"] = False
            report["errors"].append(f"too_long:{filename}:{info['line_count']}")
        missing_markers = [marker for marker in markers if marker not in text]
        if missing_markers:
            info["markers_ok"] = False
            info["missing_markers"] = missing_markers
            report["ok"] = False
            report["errors"].append(f"missing_markers:{filename}")
        report["files"][filename] = info

        if filename == "PROJECT_BRIEF.md":
            report["reentry_digest"]["current_goal"] = _extract_section(text, "## Current Goal")[:3]
            report["reentry_digest"]["architecture_snapshot"] = _extract_section(text, "## Architecture Snapshot")[:4]
        if filename == "RUNBOOK.md":
            report["reentry_digest"]["entry_steps"] = _extract_section(text, "## Entry Steps")[:4]
            report["reentry_digest"]["safe_validation"] = _extract_section(text, "## Safe Validation")[:4]
        if filename == "KNOWN_ISSUES.md":
            report["reentry_digest"]["known_issues_headlines"] = [
                line.strip("# ").strip()
                for line in text.splitlines()
                if line.startswith("### ")
            ][:4]
    return report


def main() -> int:
    payload = {
        "ok": True,
        "projects": {},
    }
    for name, path in _project_roots().items():
        project_payload = _project_report(name, path)
        payload["projects"][name] = project_payload
        payload["ok"] = payload["ok"] and bool(project_payload["ok"])

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": payload["ok"], "report": str(REPORT_PATH)}, ensure_ascii=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
