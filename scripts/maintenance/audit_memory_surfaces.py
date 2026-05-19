#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
HOME = Path.home()
REPORT_PATH = ROOT / "reports" / "memory_surface_audit.json"


def _token_estimate(text: str) -> int:
    return max(1, len(text) // 4)


def _audit_file(path: Path, warn_tokens: int) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {"path": str(resolved), "exists": False, "warn": False}
    text = resolved.read_text(encoding="utf-8", errors="replace")
    tokens = _token_estimate(text)
    return {
        "path": str(resolved),
        "exists": True,
        "size_bytes": resolved.stat().st_size,
        "line_count": len(text.splitlines()),
        "estimated_tokens": tokens,
        "warn": tokens >= warn_tokens,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only audit for Codex and AGN memory surfaces")
    parser.add_argument("--warn-tokens", type=int, default=5000)
    args = parser.parse_args()

    targets = [
        HOME / ".codex" / "MACHINE_CONTEXT.md",
        HOME / ".codex" / "RECENT_MACHINE_SETUP.md",
        ROOT / "README.md",
        ROOT / "PROJECT_BRIEF.md",
        ROOT / "RUNBOOK.md",
        ROOT / "KNOWN_ISSUES.md",
    ]
    audited = [_audit_file(path, int(args.warn_tokens)) for path in targets]
    warnings = [item for item in audited if item.get("warn")]
    summary = {
        "ok": True,
        "warn_threshold_tokens": int(args.warn_tokens),
        "warnings": warnings,
        "files": audited,
        "policy": [
            "This is a read-only auditor. It does not summarize or rewrite memory files.",
            "If a file crosses the warning threshold, prune or archive manually after review.",
        ],
    }
    _write_json(REPORT_PATH, summary)
    print(json.dumps({"ok": True, "report": str(REPORT_PATH), "warning_count": len(warnings)}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
