#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "reports" / "protocol_drift_validation.json"


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _contains(path: Path, needle: str) -> bool:
    if not path.exists():
        return False
    return needle in path.read_text(encoding="utf-8", errors="replace")


def _portable_model_ref(value: Any) -> bool:
    raw = str(value or "").strip()
    return bool(raw) and not raw.startswith(("/Users/", "/Vol" + "umes/"))


def main() -> int:
    model_router = _load_json(ROOT / "config" / "model_router.json")
    providers = _load_json(ROOT / "config" / "providers.json")
    readme = ROOT / "README.md"
    runbook = ROOT / "RUNBOOK.md"
    security_doc = ROOT / "SECURITY.md"
    commands_doc = ROOT / "documentation" / "reference" / "commands.md"

    order = model_router.get("default_provider_order", [])
    qwen_exec = providers.get("executors", {}).get("qwen_local", {})
    deepseek_reviewer = providers.get("reviewers", {}).get("deepseek", {})

    checks = {
        "provider_order_matches_policy": order == ["qwen_local", "deepseek", "gemini", "claude"],
        "qwen_default_model_is_portable": _portable_model_ref(qwen_exec.get("default_model", "")),
        "deepseek_registered": bool(deepseek_reviewer),
        "readme_mentions_core_commands": _contains(readme, "## Core Commands"),
        "runbook_mentions_lifecycle": _contains(runbook, "## Lifecycle"),
        "security_doc_exists": security_doc.exists(),
        "commands_doc_exists": commands_doc.exists(),
    }

    summary = {
        "ok": all(checks.values()),
        "checks": checks,
        "artifacts": {"report": str(REPORT_PATH)},
    }
    _write_json(REPORT_PATH, summary)
    print(json.dumps({"ok": summary["ok"], "report": str(REPORT_PATH)}, ensure_ascii=True))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
