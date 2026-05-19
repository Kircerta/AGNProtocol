#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def extract_json_object(raw_text: str) -> dict[str, Any] | None:
    text = raw_text.strip()
    if not text:
        return None

    try:
        decoded = json.loads(text)
        if isinstance(decoded, dict):
            return decoded
        if isinstance(decoded, list) and decoded and isinstance(decoded[0], dict):
            return decoded[0]
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidate = text[start : end + 1]
        try:
            decoded = json.loads(candidate)
            if isinstance(decoded, dict):
                return decoded
        except json.JSONDecodeError:
            return None
    return None


def run_gemini(prompt: str, timeout_sec: float) -> str:
    proc = subprocess.run(
        [
            "gemini",
            "--approval-mode",
            "yolo",
            "--output-format",
            "json",
            "--prompt",
            prompt,
        ],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=timeout_sec,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gemini failed rc={proc.returncode}")
    return proc.stdout


def fallback_payload(args: argparse.Namespace, correlation_id: str, warning: str) -> dict[str, Any]:
    return {
        "task_id": args.task_id,
        "request_text": args.text,
        "source": args.source,
        "correlation_id": correlation_id,
        "repo_path": args.repo_path,
        "work_branch": args.work_branch,
        "acceptance_criteria": [
            {"id": "AC-1", "text": "apply fix on target work branch"},
            {"id": "AC-2", "text": "attach executable verification output"},
            {"id": "AC-3", "text": "produce traceable reviewer verdict"},
        ],
        "warnings": [warning],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Create dispatch payload via Gemini, then ingest")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--repo-path", required=True)
    parser.add_argument("--work-branch", "--branch", dest="work_branch", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--source", default="gemini_dispatch")
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    args = parser.parse_args()

    correlation_id = f"corr-{uuid4().hex[:12]}"

    payload: dict[str, Any]
    if shutil.which("gemini") is None:
        payload = fallback_payload(args, correlation_id, "gemini_not_installed")
    else:
        prompt = (
            "Return ONLY one JSON object with keys: "
            "task_id, request_text, source, correlation_id, repo_path, work_branch, acceptance_criteria. "
            "acceptance_criteria must be an array of at least 3 entries. "
            "No markdown and no explanation.\n"
            f"task_id={args.task_id}\n"
            f"request_text={args.text}\n"
            f"source={args.source}\n"
            f"correlation_id={correlation_id}\n"
            f"repo_path={args.repo_path}\n"
            f"work_branch={args.work_branch}\n"
            f"now_utc={utc_now_iso()}\n"
        )
        try:
            raw = run_gemini(prompt, args.timeout_seconds)
            decoded = extract_json_object(raw)
            if not isinstance(decoded, dict):
                raise ValueError("gemini output missing JSON object")
            payload = decoded
        except Exception as exc:
            payload = fallback_payload(args, correlation_id, f"gemini_fallback:{type(exc).__name__}")

    payload["task_id"] = args.task_id
    payload["repo_path"] = args.repo_path
    payload["work_branch"] = args.work_branch
    payload.setdefault("request_text", args.text)
    payload.setdefault("source", args.source)
    payload.setdefault("correlation_id", correlation_id)

    proc = subprocess.run(
        [sys.executable, "scripts/coordinator_ingest.py", "--from-stdin"],
        cwd=str(ROOT),
        input=json.dumps(payload, ensure_ascii=True),
        text=True,
        capture_output=True,
        timeout=60.0,
    )
    if proc.returncode != 0:
        if proc.stdout.strip():
            print(proc.stdout.strip())
        if proc.stderr.strip():
            print(proc.stderr.strip())
        return proc.returncode

    print(proc.stdout.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
