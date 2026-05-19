#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
INPUTS_DIR = ROOT / "runtime" / "low_token_tools_examples" / "inputs"
OUTPUTS_DIR = ROOT / "runtime" / "low_token_tools_examples" / "outputs"
REPORT_PATH = ROOT / "reports" / "low_token_tools_validation.json"

TOOL_RUNS = [
    ("text_normalizer", "scripts/low_token_tools/text_normalizer.py", "text_normalizer.json", "text_normalizer.qwen_local.json", 0),
    ("json_extractor", "scripts/low_token_tools/json_extractor.py", "json_extractor.json", "json_extractor.qwen_local.json", 0),
    ("label_normalizer", "scripts/low_token_tools/label_normalizer.py", "label_normalizer.json", "label_normalizer.qwen_local.json", 0),
    ("ocr_post_cleaner", "scripts/low_token_tools/ocr_post_cleaner.py", "ocr_post_cleaner.json", "ocr_post_cleaner.qwen_local.json", 0),
    ("batch_record_cleaner", "scripts/low_token_tools/batch_record_cleaner.py", "batch_record_cleaner.json", "batch_record_cleaner.qwen_local.json", 0),
]


def _run_script(*, script_path: str, input_name: str, output_name: str, sample_size: int = 0) -> dict[str, Any]:
    output_path = OUTPUTS_DIR / output_name
    cmd = [
        sys.executable,
        script_path,
        "--input",
        str(INPUTS_DIR / input_name),
        "--output",
        str(output_path),
        "--provider",
        "qwen_local",
    ]
    if sample_size > 0:
        cmd.extend(["--sample-size", str(sample_size)])
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=240,
    )
    payload: dict[str, Any] = {}
    if output_path.exists():
        try:
            loaded = json.loads(output_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except Exception:
            payload = {}
    return {
        "command": cmd,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "output_path": str(output_path),
        "payload": payload,
    }


def main() -> int:
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "ok": False,
        "provider": "qwen_local",
        "tool_runs": {},
        "sample_check": {},
        "failure_case": {},
    }

    all_ok = True
    for tool_name, script_path, input_name, output_name, sample_size in TOOL_RUNS:
        run = _run_script(
            script_path=script_path,
            input_name=input_name,
            output_name=output_name,
            sample_size=sample_size,
        )
        payload = run.get("payload", {}) if isinstance(run.get("payload", {}), dict) else {}
        ok = run["returncode"] == 0 and bool(payload.get("ok"))
        summary["tool_runs"][tool_name] = {
            "ok": ok,
            "output_path": run["output_path"],
            "sample_applied": payload.get("sample_applied", False),
        }
        all_ok = all_ok and ok

    sample_run = _run_script(
        script_path="scripts/low_token_tools/batch_record_cleaner.py",
        input_name="batch_record_cleaner.json",
        output_name="batch_record_cleaner.sample3.qwen_local.json",
        sample_size=3,
    )
    sample_payload = sample_run.get("payload", {}) if isinstance(sample_run.get("payload", {}), dict) else {}
    sample_ok = sample_run["returncode"] == 0 and bool(sample_payload.get("ok")) and bool(sample_payload.get("sample_applied"))
    summary["sample_check"] = {
        "ok": sample_ok,
        "output_path": sample_run["output_path"],
        "sample_applied": sample_payload.get("sample_applied", False),
        "cleaned_count": ((sample_payload.get("result", {}) or {}).get("summary", {}) or {}).get("cleaned_count"),
    }
    all_ok = all_ok and sample_ok

    failure_run = _run_script(
        script_path="scripts/low_token_tools/label_normalizer.py",
        input_name="failure_invalid_label_normalizer.json",
        output_name="failure_invalid_label_normalizer.qwen_local.json",
        sample_size=0,
    )
    failure_payload = failure_run.get("payload", {}) if isinstance(failure_run.get("payload", {}), dict) else {}
    failure_ok = failure_run["returncode"] != 0 and not bool(failure_payload.get("ok")) and bool(failure_payload.get("errors"))
    summary["failure_case"] = {
        "ok": failure_ok,
        "output_path": failure_run["output_path"],
        "errors": failure_payload.get("errors", []),
    }
    all_ok = all_ok and failure_ok

    summary["ok"] = all_ok
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(summary, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": summary["ok"], "report": str(REPORT_PATH)}, ensure_ascii=True))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
