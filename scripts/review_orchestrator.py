#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from agn.governance.review_contract import (
    extract_json_object,
    merge_structured_verdicts,
    normalize_structured_verdict,
    structured_verdict_schema,
)
from agn_handler_cli_guard import render_direct_handler_cli_block, should_block_direct_handler_cli

REPORT_DIR = ROOT / "reports" / "review_orchestrator"


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _target_excerpt(path: Path, max_chars: int) -> str:
    text = _read_text(path)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 18] + "\n...<truncated>..."


def _review_prompt(
    *,
    file_path: Path,
    excerpt: str,
    review_goal: str,
    extra_context: str,
    max_rounds: int,
) -> str:
    schema = json.dumps(structured_verdict_schema(), ensure_ascii=True, indent=2, sort_keys=True)
    context_block = f"\nExtra context:\n{extra_context.strip()}\n" if extra_context.strip() else ""
    return (
        "Review the actual local file in the workspace, not just the excerpt below. "
        "Use the excerpt only as a quick entry point.\n\n"
        f"Goal:\n{review_goal.strip()}\n"
        f"{context_block}\n"
        f"Primary target file:\n{file_path}\n\n"
        "Review priorities:\n"
        "- correctness regressions\n"
        "- routing, validation, or safety drift\n"
        "- hidden operational fragility\n"
        "- prompt-injection or exfiltration risk if relevant\n\n"
        "Output rules:\n"
        "- Return exactly one JSON object and nothing else.\n"
        "- Default to a single review round.\n"
        f"- This run allows at most {max_rounds} review round(s).\n"
        "- Use verdict=approve only when the evidence is sufficient.\n"
        "- Use verdict=revise when one bounded correction should be attempted.\n"
        "- Use verdict=reject when the target should not pass in its current state.\n"
        "- Use verdict=escalate when the human must intervene.\n\n"
        f"Required schema:\n{schema}\n\n"
        f"Quick excerpt:\n```text\n{excerpt}\n```"
    )


def _command_for_provider(*, provider: str, model: str, prompt: str, include_dir: Path) -> list[str]:
    if provider == "claude":
        cmd = [
            "claude",
            "-p",
            "--permission-mode",
            "plan",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(structured_verdict_schema(), ensure_ascii=True),
            "--add-dir",
            str(include_dir),
        ]
        if model:
            cmd.extend(["--model", model])
        cmd.append(prompt)
        return cmd
    if provider == "gemini":
        cmd = [
            "gemini",
            "-p",
            prompt,
            "--output-format",
            "text",
            "--include-directories",
            str(include_dir),
        ]
        if model:
            cmd.extend(["--model", model])
        return cmd
    raise ValueError(f"unsupported_provider:{provider}")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _raw_log_paths(file_path: Path, provider: str, run_id: str) -> tuple[Path, Path]:
    stem = file_path.stem
    return (
        REPORT_DIR / f"{stem}.{run_id}.{provider}.stdout.log",
        REPORT_DIR / f"{stem}.{run_id}.{provider}.stderr.log",
    )


def _run_parallel_with_logging(
    *,
    commands: dict[str, list[str]],
    file_path: Path,
    run_id: str,
    timeout_sec: float,
    report_path: Path,
    base_payload: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    started = time.perf_counter()
    processes: dict[str, subprocess.Popen[str]] = {}
    stdout_chunks: dict[str, list[str]] = {}
    stderr_chunks: dict[str, list[str]] = {}
    results: dict[str, dict[str, Any]] = {}
    stdout_logs: dict[str, Path] = {}
    stderr_logs: dict[str, Path] = {}
    lock = threading.Lock()

    def flush_partial() -> None:
        partial = dict(base_payload)
        partial["status"] = "running"
        partial["updated_at"] = utc_now_iso()
        partial["providers"] = {}
        for provider in commands:
            stdout_text = "".join(stdout_chunks.get(provider, []))
            stderr_text = "".join(stderr_chunks.get(provider, []))
            stdout_log = stdout_logs.get(provider)
            stderr_log = stderr_logs.get(provider)
            if stdout_log:
                _write_text(stdout_log, stdout_text)
            if stderr_log:
                _write_text(stderr_log, stderr_text)
            existing = results.get(provider, {})
            partial["providers"][provider] = {
                **existing,
                "stdout_log": str(stdout_log) if stdout_log else "",
                "stderr_log": str(stderr_log) if stderr_log else "",
                "stdout_preview": stdout_text[-2000:],
                "stderr_preview": stderr_text[-2000:],
            }
        _write_json(report_path, partial)

    def collect_stream(stream: Any, bucket: list[str]) -> None:
        if stream is None:
            return
        try:
            for line in stream:
                with lock:
                    bucket.append(line)
        finally:
            stream.close()

    for name, cmd in commands.items():
        stdout_log, stderr_log = _raw_log_paths(file_path, name, run_id)
        stdout_logs[name] = stdout_log
        stderr_logs[name] = stderr_log
        stdout_chunks[name] = []
        stderr_chunks[name] = []
        try:
            processes[name] = subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1,
            )
            results[name] = {
                "state": "running",
                "returncode": None,
                "timed_out": False,
                "duration_ms": 0.0,
            }
        except FileNotFoundError as exc:
            results[name] = {
                "state": "failed_to_start",
                "returncode": 127,
                "stdout": "",
                "stderr": f"EXECUTABLE_NOT_FOUND:{exc}",
                "timed_out": False,
                "duration_ms": 0.0,
                "stdout_log": str(stdout_log),
                "stderr_log": str(stderr_log),
            }
            _write_text(stderr_log, f"EXECUTABLE_NOT_FOUND:{exc}\n")

    flush_partial()

    readers: list[threading.Thread] = []
    for name, proc in processes.items():
        stdout_thread = threading.Thread(target=collect_stream, args=(proc.stdout, stdout_chunks[name]), daemon=True)
        stderr_thread = threading.Thread(target=collect_stream, args=(proc.stderr, stderr_chunks[name]), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        readers.extend([stdout_thread, stderr_thread])

    deadline = time.time() + timeout_sec
    pending = set(processes.keys())
    while pending:
        now = time.time()
        for name in list(pending):
            proc = processes[name]
            if proc.poll() is None:
                continue
            with lock:
                stdout_text = "".join(stdout_chunks[name])
                stderr_text = "".join(stderr_chunks[name])
            _write_text(stdout_logs[name], stdout_text)
            _write_text(stderr_logs[name], stderr_text)
            results[name] = {
                "state": "completed",
                "returncode": int(proc.returncode),
                "stdout": stdout_text,
                "stderr": stderr_text,
                "timed_out": False,
                "duration_ms": round((time.perf_counter() - started) * 1000.0, 2),
                "stdout_log": str(stdout_logs[name]),
                "stderr_log": str(stderr_logs[name]),
            }
            pending.remove(name)
            flush_partial()
        if not pending:
            break
        if now >= deadline:
            for name in list(pending):
                proc = processes[name]
                proc.kill()
                proc.wait(timeout=5)
                with lock:
                    stdout_text = "".join(stdout_chunks[name])
                    stderr_text = "".join(stderr_chunks[name])
                _write_text(stdout_logs[name], stdout_text)
                _write_text(stderr_logs[name], stderr_text)
                results[name] = {
                    "state": "timed_out",
                    "returncode": 124,
                    "stdout": stdout_text,
                    "stderr": stderr_text,
                    "timed_out": True,
                    "duration_ms": round((time.perf_counter() - started) * 1000.0, 2),
                    "stdout_log": str(stdout_logs[name]),
                    "stderr_log": str(stderr_logs[name]),
                }
                pending.remove(name)
            flush_partial()
            break
        time.sleep(1.0)

    for thread in readers:
        thread.join(timeout=1.0)
    return results


def _parse_provider_verdict(result: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(result, dict):
        return None, "provider_result_missing"
    stdout = str(result.get("stdout", "") or "")
    payload = extract_json_object(stdout)
    if payload is None and isinstance(result.get("stderr"), str):
        payload = extract_json_object(str(result.get("stderr", "")))
    if payload is None:
        return None, "structured_verdict_missing"
    if isinstance(payload.get("structured_output"), dict):
        payload = dict(payload.get("structured_output") or {})
    return normalize_structured_verdict(payload), ""


def run_review(
    *,
    file_path: Path,
    include_dir: Path,
    review_goal: str,
    extra_context: str,
    claude_model: str,
    gemini_model: str,
    excerpt_chars: int,
    timeout_sec: float,
    max_rounds: int = 1,
) -> dict[str, Any]:
    excerpt = _target_excerpt(file_path, excerpt_chars)
    allowed_rounds = min(2, max(1, int(max_rounds)))
    prompt = _review_prompt(
        file_path=file_path,
        excerpt=excerpt,
        review_goal=review_goal,
        extra_context=extra_context,
        max_rounds=allowed_rounds,
    )
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = REPORT_DIR / f"{file_path.stem}.{run_id}.review.json"

    claude_cmd = _command_for_provider(provider="claude", model=claude_model, prompt=prompt, include_dir=include_dir)
    gemini_cmd = _command_for_provider(provider="gemini", model=gemini_model, prompt=prompt, include_dir=include_dir)

    base_payload = {
        "generated_at": utc_now_iso(),
        "run_id": run_id,
        "target_file": str(file_path),
        "include_dir": str(include_dir),
        "review_goal": review_goal,
        "timeout_sec": timeout_sec,
        "round_policy": {"default_rounds": 1, "max_rounds": allowed_rounds},
        "claude": {"model": claude_model, "command": claude_cmd},
        "gemini": {"model": gemini_model, "command": gemini_cmd},
    }
    parallel = _run_parallel_with_logging(
        commands={"claude": claude_cmd, "gemini": gemini_cmd},
        file_path=file_path,
        run_id=run_id,
        timeout_sec=timeout_sec,
        report_path=report_path,
        base_payload=base_payload,
    )

    providers: dict[str, Any] = {}
    verdicts: list[dict[str, Any]] = []
    for name, result in parallel.items():
        parsed, parse_error = _parse_provider_verdict(result)
        if parsed is not None:
            verdicts.append(parsed)
        providers[name] = {
            "model": claude_model if name == "claude" else gemini_model,
            "command": claude_cmd if name == "claude" else gemini_cmd,
            **result,
            "structured_verdict": parsed,
            "parse_error": parse_error,
        }

    overall_verdict = merge_structured_verdicts(verdicts)
    status = "completed" if verdicts else "failed"
    payload = {
        "generated_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "run_id": run_id,
        "target_file": str(file_path),
        "include_dir": str(include_dir),
        "review_goal": review_goal,
        "round_policy": {"default_rounds": 1, "max_rounds": allowed_rounds},
        "overall_verdict": overall_verdict,
        "providers": providers,
        "status": status,
        "report_path": str(report_path),
    }
    _write_json(report_path, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Run parallel high-tier review across Claude and Gemini on a local file")
    parser.add_argument(
        "--internal-handler-cli",
        action="store_true",
        help="Acknowledge that scripts/review_orchestrator.py is an internal handler CLI, not the preferred active AGN surface.",
    )
    parser.add_argument("--file", required=True, help="Absolute or repo-relative file to review")
    parser.add_argument("--include-dir", default=str(ROOT), help="Directory the reviewers may inspect")
    parser.add_argument("--goal", default="Review this file for correctness, safety, and operational fragility.")
    parser.add_argument("--extra-context", default="")
    parser.add_argument("--claude-model", default="opus")
    parser.add_argument("--gemini-model", default="pro")
    parser.add_argument("--excerpt-chars", type=int, default=4000)
    parser.add_argument("--timeout-sec", type=float, default=600.0)
    parser.add_argument("--max-rounds", type=int, default=1)
    args = parser.parse_args()
    if should_block_direct_handler_cli(bool(getattr(args, "internal_handler_cli", False))):
        print(
            render_direct_handler_cli_block(
                handler_id="review_orchestrator",
                purpose="Structured flagship review handler behind governed AGN review surfaces.",
                recommended_entrypoints=[
                    "python3 scripts/agn2_execution_workflow.py review --file <path> --goal \"...\"",
                    "python3 scripts/agn_governed_execution.py review --file <path> --goal \"...\"",
                ],
                notes=[
                    "Use the explicit override flag only for validation, compatibility, or implementation-level inspection.",
                    "Active AGN review should preserve dispatcher/governed execution posture.",
                ],
            )
        )
        return 2

    file_path = Path(args.file)
    if not file_path.is_absolute():
        file_path = (ROOT / file_path).resolve()
    include_dir = Path(args.include_dir)
    if not include_dir.is_absolute():
        include_dir = (ROOT / include_dir).resolve()
    if not file_path.exists():
        print(json.dumps({"ok": False, "error": f"file_not_found:{file_path}"}, ensure_ascii=True))
        return 1

    payload = run_review(
        file_path=file_path,
        include_dir=include_dir,
        review_goal=str(args.goal),
        extra_context=str(args.extra_context),
        claude_model=str(args.claude_model).strip(),
        gemini_model=str(args.gemini_model).strip(),
        excerpt_chars=max(1000, int(args.excerpt_chars)),
        timeout_sec=max(30.0, float(args.timeout_sec)),
        max_rounds=min(2, max(1, int(args.max_rounds))),
    )
    print(json.dumps({"ok": True, "report_path": payload["report_path"], "run_id": payload["run_id"], "status": payload["status"]}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
