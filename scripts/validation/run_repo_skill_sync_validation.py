#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from admin_control_common import atomic_write_json


REPORT_DIR = ROOT / "reports" / "validation"
TEST_COMMAND = ["uvx", "pytest", "tests/test_repo_skill_portability.py", "-q"]
FORBIDDEN_FRAGMENTS = [
    str(Path.home()),
    "<repo-root>",
]
SCAN_PATHS = [
    ROOT / "AGENTS.md",
    ROOT / "RUNBOOK.md",
    ROOT / "documentation" / "admin" / "CODEX_PERSONALIZATION_AGENT.md",
    ROOT / "documentation" / "reference" / "agn2_codex_operating_memory.md",
]
TEXT_SUFFIXES = {
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".sh",
    ".swift",
    ".txt",
    ".yaml",
    ".yml",
}


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _run(cmd: list[str], *, cwd: Path | None = None, timeout_sec: float = 300.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd or ROOT),
        text=True,
        capture_output=True,
        timeout=timeout_sec,
        check=False,
    )


def _parse_passed_count(stdout: str) -> int:
    for line in stdout.splitlines():
        if " passed" in line:
            first = line.strip().split()[0]
            if first.isdigit():
                return int(first)
    return 0


def _load_sync_inventory() -> dict[str, Any]:
    completed = _run(["python3", "scripts/sync_repo_skills.py", "list", "--json"])
    payload: dict[str, Any] = {}
    parse_error = ""
    if completed.returncode == 0:
        try:
            payload = json.loads(str(completed.stdout or "{}"))
        except json.JSONDecodeError as exc:
            parse_error = f"{type(exc).__name__}: {exc}"
    return {
        "returncode": int(completed.returncode),
        "parse_error": parse_error,
        "payload": payload,
        "stdout_tail": str(completed.stdout or "").strip().splitlines()[-5:],
        "stderr_tail": str(completed.stderr or "").strip().splitlines()[-5:],
    }


def _portability_scan() -> dict[str, Any]:
    checked_files: list[str] = []
    violations: list[dict[str, str]] = []
    paths = SCAN_PATHS + sorted((ROOT / "skills").rglob("*"))
    for path in paths:
        if not path.is_file():
            continue
        if path.suffix not in TEXT_SUFFIXES and path.name != "SKILL.md":
            continue
        checked_files.append(str(path.relative_to(ROOT)))
        text = path.read_text(encoding="utf-8")
        for fragment in FORBIDDEN_FRAGMENTS:
            if fragment in text:
                violations.append(
                    {
                        "path": str(path.relative_to(ROOT)),
                        "fragment": fragment,
                    }
                )
    return {
        "checked_count": len(checked_files),
        "violations": violations,
        "pass": not violations,
    }


def _test_state() -> dict[str, Any]:
    completed = _run(TEST_COMMAND)
    stdout = str(completed.stdout or "")
    return {
        "command": TEST_COMMAND,
        "returncode": int(completed.returncode),
        "passed_count": _parse_passed_count(stdout),
        "stdout_tail": stdout.strip().splitlines()[-5:],
        "stderr_tail": str(completed.stderr or "").strip().splitlines()[-5:],
        "pass": completed.returncode == 0,
    }


def _sync_state() -> dict[str, Any]:
    inventory = _load_sync_inventory()
    payload = inventory["payload"] if isinstance(inventory["payload"], dict) else {}
    groups = payload.get("groups", {}) if isinstance(payload, dict) else {}
    unsynced: list[dict[str, Any]] = []
    total_skill_count = int(payload.get("total_skill_count", 0)) if isinstance(payload, dict) else 0
    for group_name, group in groups.items():
        if not isinstance(group, dict):
            continue
        for skill in group.get("skills", []):
            if not isinstance(skill, dict):
                continue
            installed = bool(skill.get("installed", False))
            has_redirect_note = bool(skill.get("has_redirect_note", False))
            if not (installed and has_redirect_note):
                unsynced.append(
                    {
                        "group": group_name,
                        "name": str(skill.get("name", "")),
                        "installed": installed,
                        "has_redirect_note": has_redirect_note,
                    }
                )
    return {
        "inventory": inventory,
        "total_skill_count": total_skill_count,
        "unsynced": unsynced,
        "fully_synced_count": total_skill_count - len(unsynced),
        "pass": inventory["returncode"] == 0 and not inventory["parse_error"] and not unsynced,
    }


def _note_checks() -> dict[str, Any]:
    note_paths = [
        Path.home() / ".codex_agn" / "skills" / "agn-system-entry" / "AGN_CANONICAL_SOURCE.md",
        Path.home() / ".codex" / "skills" / "pdf" / "AGN_CANONICAL_SOURCE.md",
    ]
    checks = []
    for note_path in note_paths:
        exists = note_path.exists()
        text = note_path.read_text(encoding="utf-8") if exists else ""
        checks.append(
            {
                "path": str(note_path),
                "exists": exists,
                "has_repo_relative_source": "canonical source: `skills/" in text,
                "has_repo_root_command": "python3 scripts/sync_repo_skills.py install" in text,
                "has_home_relative_target": "installed copy: `~/" in text,
                "pass": exists
                and "canonical source: `skills/" in text
                and "python3 scripts/sync_repo_skills.py install" in text
                and "installed copy: `~/" in text,
            }
        )
    return {
        "checks": checks,
        "pass": all(item["pass"] for item in checks),
    }


def build_report() -> dict[str, Any]:
    tests = _test_state()
    portability = _portability_scan()
    sync = _sync_state()
    notes = _note_checks()
    overall_pass = tests["pass"] and portability["pass"] and sync["pass"] and notes["pass"]
    return {
        "schema_version": "agn.validation.repo_skill_sync.v1",
        "generated_at": utc_now_iso(),
        "objective": "Verify that repo-side skills are machine-portable, locally synced into the current Codex homes, and documented with relative redirect notes.",
        "tests": tests,
        "portability_scan": portability,
        "sync_state": sync,
        "redirect_notes": notes,
        "overall_pass": overall_pass,
    }


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = build_report()
    report_path = REPORT_DIR / f"{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-repo-skill-sync-validation.json"
    atomic_write_json(report_path, report)
    print(json.dumps(report, indent=2))
    print(f"\nreport_path={report_path}")
    return 0 if report["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
