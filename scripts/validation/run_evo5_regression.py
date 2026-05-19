#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
REPORTS_DIR = ROOT / "reports"
DOC_DIR = ROOT / "documentation" / "admin"


@dataclass
class Case:
    case_id: str
    command: str
    expected: str


@dataclass
class CaseResult:
    case_id: str
    command: str
    expected: str
    actual: str
    passed: bool
    evidence_refs: list[str]


CASES = [
    Case(
        case_id="E5.T1",
        command="pytest -q tests/test_evo5_delivery_gate.py",
        expected="Delivery Gate blocks without evidence, passes with evidence, and loopback actions are generated.",
    ),
    Case(
        case_id="E5.T2",
        command="pytest -q tests/test_evo5_recovery_policy.py",
        expected="Recovery retries then degrades, and escalation transitions task to NEED_ADMIN.",
    ),
    Case(
        case_id="E5.T3",
        command="pytest -q tests/test_evo5_lifecycle_governance.py",
        expected="Integrity sweep detects missing artifact and lifecycle index includes delivered runs.",
    ),
    Case(
        case_id="E5.T4",
        command="python3 scripts/lifecycle_governance.py integrity_sweep",
        expected="Lifecycle integrity sweep emits structured report and returns JSON status.",
    ),
]


def _run(command: str) -> subprocess.CompletedProcess[str]:
    argv = shlex.split(command)
    if argv and argv[0] == "pytest" and shutil.which("pytest") is None and shutil.which("uv"):
        argv = ["uv", "run", "--with", "pytest", *argv]
    return subprocess.run(argv, cwd=str(ROOT), text=True, capture_output=True, check=False)


def _tail(text: str, max_chars: int = 600) -> str:
    raw = str(text or "").strip()
    if len(raw) <= max_chars:
        return raw
    return raw[-max_chars:]


def _render_markdown(*, results: list[CaseResult], json_path: str) -> str:
    lines: list[str] = []
    lines.append(f"# AGN Evo5 Regression Report ({datetime.now(tz=timezone.utc).date().isoformat()})")
    lines.append("")
    passed = sum(1 for item in results if item.passed)
    lines.append(f"- total: {len(results)}")
    lines.append(f"- passed: {passed}")
    lines.append(f"- failed: {len(results) - passed}")
    lines.append(f"- json_report: `{json_path}`")
    lines.append("")
    lines.append("## Cases")
    lines.append("")

    for item in results:
        lines.append(f"### {item.case_id}")
        lines.append(f"- command: `{item.command}`")
        lines.append(f"- expected: {item.expected}")
        lines.append(f"- actual: {item.actual}")
        lines.append(f"- passed: {item.passed}")
        lines.append(f"- evidence_refs: {', '.join(item.evidence_refs) if item.evidence_refs else '(none)' }")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    started = time.time()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    DOC_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")

    results: list[CaseResult] = []
    for case in CASES:
        proc = _run(case.command)
        ok = proc.returncode == 0
        case_label = case.case_id.replace(".", "_")
        case_log = REPORTS_DIR / f"evo5_{case_label}_{stamp}.log"
        case_log.write_text(
            "\n".join(
                [
                    f"command: {case.command}",
                    f"returncode: {proc.returncode}",
                    "--- stdout ---",
                    proc.stdout or "",
                    "--- stderr ---",
                    proc.stderr or "",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        evidence_refs: list[str] = [str(case_log.relative_to(ROOT))]
        actual = f"rc={proc.returncode}"

        if case.case_id == "E5.T4":
            payload: dict[str, Any] = {}
            try:
                payload = json.loads((proc.stdout or "").strip().splitlines()[-1])
            except Exception:
                payload = {}
            report = str(payload.get("report", "")).strip()
            if report:
                evidence_refs.append(report)
            report_ok = bool(report) and (ROOT / report).exists()
            has_missing_count = "missing_count" in payload
            ok = report_ok and has_missing_count
            actual = (
                f"rc={proc.returncode} report={report} report_ok={report_ok} "
                f"missing_count={payload.get('missing_count')}"
            )
        else:
            actual = f"rc={proc.returncode} tail={_tail(proc.stdout or proc.stderr)}"

        results.append(
            CaseResult(
                case_id=case.case_id,
                command=case.command,
                expected=case.expected,
                actual=actual,
                passed=ok,
                evidence_refs=evidence_refs,
            )
        )

    json_path = REPORTS_DIR / f"evo5_regression_{stamp}.json"
    payload = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "duration_sec": round(time.time() - started, 3),
        "totals": {
            "all": len(results),
            "passed": sum(1 for item in results if item.passed),
            "failed": sum(1 for item in results if not item.passed),
        },
        "cases": [asdict(item) for item in results],
        "path_mode": "relative_to_repo_root",
        "repo_root": ".",
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    date_label = datetime.now(tz=timezone.utc).date().isoformat()
    md_path = DOC_DIR / f"AGN_Evo5_Regression_Report_{date_label}.md"
    md_path.write_text(
        _render_markdown(results=results, json_path=str(json_path.relative_to(ROOT))),
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "ok": payload["totals"]["failed"] == 0,
                "json_report": str(json_path.relative_to(ROOT)),
                "markdown_report": str(md_path.relative_to(ROOT)),
                "totals": payload["totals"],
            },
            ensure_ascii=True,
        )
    )
    return 0 if payload["totals"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
