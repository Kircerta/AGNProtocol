#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "reports" / "high_risk_safety_validation.json"
REHEARSAL_DIR = ROOT / ".agn_workspace" / "high_risk_rehearsal"
MANIFEST_PATH = ROOT / "runtime" / "high_risk_examples" / "rename_manifest.json"


def _run(cmd: list[str]) -> dict[str, Any]:
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=120,
    )
    return {
        "command": cmd,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _prepare_rehearsal_dir() -> dict[str, Path]:
    if REHEARSAL_DIR.exists():
        shutil.rmtree(REHEARSAL_DIR)
    delete_dir = REHEARSAL_DIR / "delete_case"
    rename_dir = REHEARSAL_DIR / "rename_case"
    config_dir = REHEARSAL_DIR / "config_case"
    for path in (delete_dir, rename_dir, config_dir):
        path.mkdir(parents=True, exist_ok=True)
    (delete_dir / "old.log").write_text("delete me\n", encoding="utf-8")
    (delete_dir / "keep.txt").write_text("keep me\n", encoding="utf-8")
    (rename_dir / "Draft One.txt").write_text("one\n", encoding="utf-8")
    (rename_dir / "Draft Two.txt").write_text("two\n", encoding="utf-8")
    (config_dir / "test.conf").write_text("enabled=true\n", encoding="utf-8")
    return {
        "delete_dir": delete_dir,
        "rename_dir": rename_dir,
        "config_path": config_dir / "test.conf",
    }


def main() -> int:
    paths = _prepare_rehearsal_dir()
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(
            {
                "operations": [
                    {
                        "from": str((paths["rename_dir"] / "Draft One.txt").resolve()),
                        "to": str((paths["rename_dir"] / "draft-one.txt").resolve()),
                    },
                    {
                        "from": str((paths["rename_dir"] / "Draft Two.txt").resolve()),
                        "to": str((paths["rename_dir"] / "draft-two.txt").resolve()),
                    },
                ]
            },
            ensure_ascii=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    delete_plan = ROOT / "reports" / "high_risk_delete_plan.json"
    rename_plan = ROOT / "reports" / "high_risk_rename_plan.json"
    config_plan = ROOT / "reports" / "high_risk_config_plan.json"
    publish_denied_plan = ROOT / "reports" / "high_risk_publish_denied_plan.json"
    publish_approved_plan = ROOT / "reports" / "high_risk_publish_approved_plan.json"

    runs = {
        "delete": _run(
            [
                sys.executable,
                "scripts/safety/high_risk_guardrails.py",
                "plan-delete",
                "--root",
                str(paths["delete_dir"]),
                "--pattern",
                "*.log",
                "--output",
                str(delete_plan),
            ]
        ),
        "rename": _run(
            [
                sys.executable,
                "scripts/safety/high_risk_guardrails.py",
                "plan-rename",
                "--manifest",
                str(MANIFEST_PATH),
                "--output",
                str(rename_plan),
            ]
        ),
        "config": _run(
            [
                sys.executable,
                "scripts/safety/high_risk_guardrails.py",
                "plan-config-change",
                "--target-path",
                str(paths["config_path"]),
                "--backup-path",
                str(paths["config_path"]) + ".bak",
                "--change-summary",
                "Switch test flag during rehearsal",
                "--output",
                str(config_plan),
            ]
        ),
        "publish_denied": _run(
            [
                sys.executable,
                "scripts/safety/high_risk_guardrails.py",
                "plan-publish",
                "--repo-path",
                str(ROOT),
                "--remote",
                "origin",
                "--branch",
                "main",
                "--file",
                "README.md",
                "--output",
                str(publish_denied_plan),
            ]
        ),
        "publish_approved": _run(
            [
                sys.executable,
                "scripts/safety/high_risk_guardrails.py",
                "plan-publish",
                "--repo-path",
                str(ROOT),
                "--remote",
                "origin",
                "--branch",
                "main",
                "--file",
                "README.md",
                "--allow-external-publish",
                "--admin-approved",
                "--output",
                str(publish_approved_plan),
            ]
        ),
    }

    delete_payload = _load_json(delete_plan)
    rename_payload = _load_json(rename_plan)
    config_payload = _load_json(config_plan)
    publish_denied_payload = _load_json(publish_denied_plan)
    publish_approved_payload = _load_json(publish_approved_plan)

    checks = {
        "delete_ok": runs["delete"]["returncode"] == 0 and delete_payload.get("dry_run") is True and delete_payload.get("match_count") == 1,
        "rename_ok": runs["rename"]["returncode"] == 0 and rename_payload.get("operation_count") == 2,
        "config_ok": runs["config"]["returncode"] == 0 and config_payload.get("requires_backup") is True,
        "publish_denied_ok": runs["publish_denied"]["returncode"] != 0 and publish_denied_payload.get("guardrail_status") == "blocked",
        "publish_approved_ok": runs["publish_approved"]["returncode"] == 0 and publish_approved_payload.get("guardrail_status") == "ready_for_confirmation",
    }

    summary = {
        "ok": all(checks.values()),
        "checks": checks,
        "artifacts": {
            "delete_plan": str(delete_plan),
            "rename_plan": str(rename_plan),
            "config_plan": str(config_plan),
            "publish_denied_plan": str(publish_denied_plan),
            "publish_approved_plan": str(publish_approved_plan),
            "rename_manifest": str(MANIFEST_PATH),
        },
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(summary, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": summary["ok"], "report": str(REPORT_PATH)}, ensure_ascii=True))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
