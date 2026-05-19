#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "reports" / "execution_protocol_validation.json"

REQUIRED_FILES = {
    "documentation/reference/commands.md": [
        "## Setup",
        "## Lifecycle",
        "## Task Start",
        "## API",
    ],
    "documentation/templates/validation/VALIDATION_CHECKLIST.template.md": [
        "# VALIDATION_CHECKLIST",
        "## Pre-Change Checks",
        "## Commands To Run",
        "## Evidence Targets",
    ],
    "documentation/templates/validation/DELIVERY_EVIDENCE.template.md": [
        "# DELIVERY_EVIDENCE",
        "## Pre-Change Checks",
        "## Commands Run",
        "## Artifacts",
        "## Residual Risk",
    ],
}


def _validate_file(path: Path, markers: list[str]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "exists": path.exists(),
        "markers_ok": True,
        "missing_markers": [],
    }
    if not path.exists():
        payload["markers_ok"] = False
        payload["missing_markers"] = list(markers)
        return payload
    text = path.read_text(encoding="utf-8")
    missing = [marker for marker in markers if marker not in text]
    if missing:
        payload["markers_ok"] = False
        payload["missing_markers"] = missing
    return payload


def main() -> int:
    summary: dict[str, Any] = {"ok": True, "files": {}}
    for rel_path, markers in REQUIRED_FILES.items():
        info = _validate_file(ROOT / rel_path, markers)
        summary["files"][rel_path] = info
        summary["ok"] = summary["ok"] and bool(info["exists"]) and bool(info["markers_ok"])

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(summary, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": summary["ok"], "report": str(REPORT_PATH)}, ensure_ascii=True))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
