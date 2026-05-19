#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

ALLOWLIST = {
    "scripts/agn_host_state_probe.py",
    "scripts/agn_refs.py",
    "scripts/maintenance/check_portability.py",
    "scripts/validation/run_protocol_drift_validation.py",
    "scripts/validation/run_repo_skill_sync_validation.py",
    "tests/test_agn_console_ref_read.py",
    "tests/test_evo4_acceptance_placeholders.py",
    "tests/test_repo_skill_portability.py",
}

TEXT_EXTENSIONS = {
    "",
    ".cfg",
    ".env",
    ".gitignore",
    ".ini",
    ".json",
    ".md",
    ".mk",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("mac_home", re.compile(r"/Users/[^\s\"'`]+")),
    ("mac_volume", re.compile(r"/Vol" + r"umes/[^\s\"'`]+")),
    ("windows_drive", re.compile(r"[A-Za-z]:\\\\[^\s\"'`]+")),
    ("file_uri", re.compile(r"file://[^\s\"'`]+")),
]


def _tracked_files() -> list[Path]:
    out = subprocess.check_output(["git", "ls-files"], cwd=str(ROOT), text=True)
    files: list[Path] = []
    for line in out.splitlines():
        rel = line.strip()
        if not rel:
            continue
        if rel in ALLOWLIST:
            continue
        files.append(ROOT / rel)
    return files


def _is_text_candidate(path: Path) -> bool:
    if path.suffix.lower() in TEXT_EXTENSIONS:
        return True
    return path.name in {"Makefile"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Check tracked files for absolute-path portability leaks.")
    parser.add_argument("--include-archive", action="store_true", help="Also scan documentation/archive/")
    args = parser.parse_args()

    findings: list[str] = []

    for path in _tracked_files():
        rel = path.relative_to(ROOT).as_posix()
        if not args.include_archive and rel.startswith("documentation/archive/"):
            continue
        if not path.exists() or not path.is_file():
            continue
        if not _is_text_candidate(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for label, pattern in PATTERNS:
                if pattern.search(line):
                    findings.append(f"{rel}:{lineno} [{label}] {line.strip()}")

    legacy_dir = ROOT / "documentation" / "admin" / "PastWorkingDoc"
    if legacy_dir.exists():
        findings.append("documentation/admin/PastWorkingDoc exists (legacy naming should be removed)")

    if findings:
        print("status=fail")
        print(f"findings={len(findings)}")
        for row in findings:
            print(f"- {row}")
        return 1

    print("status=pass")
    print("findings=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
