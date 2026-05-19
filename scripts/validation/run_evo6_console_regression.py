#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time

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
        case_id="E6.C1",
        command="pytest -q tests/test_agn_console_api.py",
        expected="Console read-model endpoints return consistent task/checkpoint/timeline/pending/control projections.",
    ),
    Case(
        case_id="E6.C2",
        command="pytest -q tests/test_agn_console_auth.py tests/test_agn_console_control_enqueue.py",
        expected="Control write endpoint enforces JWT and validates control payload constraints.",
    ),
    Case(
        case_id="E6.C3",
        command="pytest -q tests/test_agn_console_ref_read.py",
        expected="Ref reader enforces supported AGN refs and returns bounded excerpts with metadata.",
    ),
    Case(
        case_id="E6.C4",
        command="pytest -q tests/test_agn_console_local_only.py",
        expected="Local-only guard blocks remote hosts and allows debug override when disabled.",
    ),
    Case(
        case_id="E6.C5",
        command="pytest -q tests/test_agn_api.py::test_dashboard_route",
        expected="/dashboard serves the current AGN operator console.",
    ),
]


def _run(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=str(ROOT), shell=True, text=True, capture_output=True, check=False)


def _tail(text: str, max_chars: int = 800) -> str:
    raw = str(text or "").strip()
    if len(raw) <= max_chars:
        return raw
    return raw[-max_chars:]


def _render_markdown(*, results: list[CaseResult], json_path: str) -> str:
    lines: list[str] = []
    lines.append(f"# AGN Evo6 Console Regression Report ({datetime.now(tz=timezone.utc).date().isoformat()})")
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
        lines.append(f"- evidence_refs: {', '.join(item.evidence_refs) if item.evidence_refs else '(none)'}")
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
        case_label = case.case_id.replace(".", "_")
        case_log = REPORTS_DIR / f"evo6_{case_label}_{stamp}.log"
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

        passed = proc.returncode == 0
        actual = f"rc={proc.returncode} tail={_tail(proc.stdout or proc.stderr)}"
        results.append(
            CaseResult(
                case_id=case.case_id,
                command=case.command,
                expected=case.expected,
                actual=actual,
                passed=passed,
                evidence_refs=[str(case_log.relative_to(ROOT))],
            )
        )

    totals = {
        "all": len(results),
        "passed": sum(1 for item in results if item.passed),
        "failed": sum(1 for item in results if not item.passed),
    }

    json_path = REPORTS_DIR / f"evo6_console_regression_{stamp}.json"
    payload = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "duration_sec": round(time.time() - started, 3),
        "totals": totals,
        "cases": [asdict(item) for item in results],
        "path_mode": "relative_to_repo_root",
        "repo_root": ".",
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    date_label = datetime.now(tz=timezone.utc).date().isoformat()
    md_path = DOC_DIR / f"AGN_Evo6_Console_Regression_Report_{date_label}.md"
    md_path.write_text(
        _render_markdown(results=results, json_path=str(json_path.relative_to(ROOT))),
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "ok": totals["failed"] == 0,
                "json_report": str(json_path.relative_to(ROOT)),
                "markdown_report": str(md_path.relative_to(ROOT)),
                "totals": totals,
            },
            ensure_ascii=True,
        )
    )
    return 0 if totals["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
