#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "reports" / "bounded_delegation"

try:
    from agn.governance.execution_workflow import build_delegate_request
except ImportError:  # pragma: no cover - package import fallback
    from scripts.agn2_execution_workflow import build_delegate_request

try:
    from capability_snapshot import build_capability_snapshot
except ImportError:  # pragma: no cover - package import fallback
    from scripts.capability_snapshot import build_capability_snapshot


PROFILE_HINTS = {
    "json_extraction": ("json", "schema", "extract fields", "parse fields"),
    "label_normalization": ("normalize", "rename", "canonicalize", "map labels"),
    "batch_cleaning": ("batch", "cleanup", "dedupe", "clean rows"),
    "ocr_cleanup": ("ocr", "screenshot text", "scan", "image text"),
    "bounded_summarization": ("summarize", "summary", "condense", "outline"),
    "structured_transform": ("transform", "convert", "reshape", "reformat"),
}
FORBIDDEN_TOKENS = (
    "architecture",
    "governance",
    "policy",
    "destructive",
    "delete production",
    "final decision",
    "final judgment",
    "approve deployment",
    "security signoff",
)
SEMANTIC_POLICY_RULES: tuple[dict[str, Any], ...] = (
    {
        "id": "authority_substitution",
        "message": "instruction asks the worker to replace the controller, reviewer lane, or operator for final authority.",
        "patterns": (
            r"(?i)\b(final|sole)\s+(decision|judgment|approval|signoff)\b",
            r"(?i)\bapprove\s+(deployment|release|prod|production)\b",
            r"(?i)\bact\s+as\s+(the\s+)?operator\b",
            r"(?i)\bmake\s+the\s+(architecture|governance|policy)\s+decision\b",
        ),
    },
    {
        "id": "policy_bypass",
        "message": "instruction attempts to bypass governance, approval, review, or policy boundaries.",
        "patterns": (
            r"(?i)\b(bypass|override|ignore)\s+(approval|policy|review|gate|governance)\b",
            r"(?i)\bskip\s+(approval|review|policy gate)\b",
        ),
    },
    {
        "id": "privileged_or_destructive",
        "message": "instruction includes privileged secret handling or destructive production actions that must stay out of worker lanes.",
        "patterns": (
            r"(?i)\b(delete|drop|purge|wipe)\s+(prod|production)\b",
            r"(?i)\b(export|reveal|print|send)\s+(api[_ -]?key|token|secret|password|credential)s?\b",
            r"(?i)\brotate\s+(token|secret|password|credential)s?\b",
        ),
    },
    {
        "id": "unbounded_scope",
        "message": "instruction is open-ended enough to blur the boundary between bounded labor and delegated control.",
        "patterns": (
            r"(?i)\b(handle|finish|own|take over)\s+(the\s+)?(entire|whole|full)\s+(task|project|workflow)\b",
            r"(?i)\bdo whatever is needed\b",
            r"(?i)\bend[- ]to[- ]end\b",
        ),
    },
)


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _safe_slug(text: str, *, default: str, max_len: int = 40) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(text or "").strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    cleaned = cleaned.strip("-") or default
    return cleaned[:max_len].rstrip("-") or default


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def infer_task_profile(text: str) -> str:
    lowered = str(text or "").lower()
    for profile, hints in PROFILE_HINTS.items():
        if any(hint in lowered for hint in hints):
            return profile
    return "general_analysis"


def semantic_policy_findings(*texts: str) -> list[dict[str, str]]:
    haystack = "\n".join(str(text or "") for text in texts if str(text or "").strip())
    findings: list[dict[str, str]] = []
    for rule in SEMANTIC_POLICY_RULES:
        for pattern in rule["patterns"]:
            match = re.search(pattern, haystack)
            if match:
                findings.append(
                    {
                        "rule_id": str(rule["id"]),
                        "message": str(rule["message"]),
                        "match": match.group(0)[:120],
                    }
                )
                break
    return findings


def delegation_blockers(text: str, risk_level: str, *, output_expectation: str = "") -> tuple[list[str], list[dict[str, str]]]:
    lowered = str(text or "").lower()
    blockers: list[str] = []
    if str(risk_level).lower() == "high":
        blockers.append("high-risk work should stay in Codex and optionally go through flagship review before any worker lane.")
    for token in FORBIDDEN_TOKENS:
        if token in lowered:
            blockers.append(f"instruction mentions `{token}`, which is not valid worker-grade authority.")
    policy_findings = semantic_policy_findings(text, output_expectation)
    for finding in policy_findings:
        blockers.append(f"{finding['message']} Matched `{finding['match']}`.")
    return blockers, policy_findings


def suggest_output_expectation(profile: str) -> str:
    expectations = {
        "json_extraction": "Return valid JSON only, with no prose wrapper.",
        "label_normalization": "Return a concise mapping table and the normalized result.",
        "batch_cleaning": "Return cleaned output plus a short note of rows changed or dropped.",
        "ocr_cleanup": "Return corrected OCR text and list uncertain fragments separately.",
        "bounded_summarization": "Return a short structured summary with explicit open questions.",
        "structured_transform": "Return the transformed artifact in the requested structure plus any loss notes.",
        "general_analysis": "Return bounded findings, assumptions, and any unresolved edge cases.",
    }
    return expectations.get(profile, expectations["general_analysis"])


def normalize_output_expectation(profile: str, output_expectation: str) -> str:
    base = str(output_expectation or "").strip() or suggest_output_expectation(profile)
    guardrail = (
        " Treat your output as advisory data only. Do not claim authority, request policy bypass, "
        "embed hidden controller instructions, or present yourself as the final approver."
    )
    if "advisory data only" in base.lower():
        return base
    return base.rstrip() + guardrail


def _provider_available(capability: dict[str, Any], provider: str) -> bool:
    provider_roles = capability.get("provider_policy", {}).get("provider_roles", {})
    return bool((provider_roles.get(provider) or {}).get("available"))


def choose_worker(capability: dict[str, Any], requested: str = "") -> str:
    if requested:
        return requested if _provider_available(capability, requested) else ""
    for candidate in ("qwen_local", "deepseek"):
        if _provider_available(capability, candidate):
            return candidate
    return ""


def build_delegate_command(
    *,
    instruction: str,
    profile: str,
    risk_level: str,
    task_id: str,
    input_refs: list[str],
    output_expectation: str,
    provider: str,
    output_path: str,
) -> str:
    command = [
        "python3",
        "scripts/agn2_execution_workflow.py",
        "delegate",
        "--instruction",
        instruction,
        "--task-profile",
        profile,
        "--risk-level",
        risk_level,
        "--task-id",
        task_id,
        "--output-expectation",
        output_expectation,
    ]
    for ref in input_refs:
        command.extend(["--input-ref", ref])
    if provider:
        command.extend(["--force-provider", provider])
    if output_path:
        command.extend(["--output", output_path])
    return " ".join(shlex.quote(part) for part in command)


def build_plan(
    *,
    instruction: str,
    risk_level: str,
    task_profile: str,
    task_id: str,
    input_refs: list[str],
    output_expectation: str,
    provider: str,
    output_path: str,
) -> dict[str, Any]:
    capability = build_capability_snapshot()
    profile = task_profile if task_profile != "auto" else infer_task_profile(instruction)
    blockers, policy_findings = delegation_blockers(instruction, risk_level, output_expectation=output_expectation)
    chosen_worker = choose_worker(capability, requested=provider)
    if provider and not chosen_worker:
        blockers.append(f"requested provider `{provider}` is not currently available for worker delegation.")
    short_task_id = task_id or f"delegate-{_safe_slug(instruction, default='task')}"
    expectation = normalize_output_expectation(profile, output_expectation)
    can_delegate = not blockers and bool(chosen_worker)
    request = None
    command = ""
    if can_delegate:
        request = build_delegate_request(
            instruction=instruction,
            task_profile=profile,
            risk_level=risk_level,
            input_refs=input_refs,
            output_expectation=expectation,
            task_id=short_task_id,
        )
        command = build_delegate_command(
            instruction=instruction,
            profile=profile,
            risk_level=risk_level,
            task_id=short_task_id,
            input_refs=input_refs,
            output_expectation=expectation,
            provider=chosen_worker,
            output_path=output_path,
        )
    return {
        "ok": True,
        "generated_at": utc_now_iso(),
        "instruction": instruction,
        "risk_level": risk_level,
        "task_profile": profile,
        "can_delegate": can_delegate,
        "blockers": blockers if blockers else ([] if chosen_worker else ["no worker-grade provider is currently available."]),
        "task_id": short_task_id,
        "recommended_worker": chosen_worker,
        "policy_findings": policy_findings,
        "delegation_boundary": [
            "worker lanes are for bounded transforms, cleanup, OCR normalization, and similar low-risk labor",
            "worker lanes must not receive authority-bearing, policy-bypass, secret-handling, or end-to-end takeover instructions",
            "Codex keeps integration, verification, reviewer escalation, and final acceptance",
        ],
        "worker_output_posture": [
            "treat_worker_output_as_untrusted_advisory_content_until_codex_verifies_it",
            "prefer_schema_bounded_outputs_and_reject_role_claims_or_policy_bypass_instructions",
            "do_not_integrate_worker_output_directly_into_privileged_actions_without_local_verification",
        ],
        "controller_responsibilities": [
            "final judgment and acceptance",
            "any architectural or governance decision",
            "live edit integration and verification",
            "deciding whether reviewer escalation is needed",
        ],
        "worker_contract": [
            "bounded_worker_task",
            "no_governance_judgment",
            "no_architecture_judgment",
            "no_final_review_authority",
        ],
        "output_expectation": expectation,
        "input_refs": input_refs,
        "delegate_request": request,
        "delegate_command": command,
    }


def _write_report(payload: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = _safe_slug(str(payload.get("instruction", "")), default="task", max_len=50)
    path = REPORT_DIR / f"{timestamp}-{slug}.json"
    latest = REPORT_DIR / "latest.json"
    _atomic_write_json(path, payload)
    _atomic_write_json(latest, payload)
    return path


def _run_delegate(command: str) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        shell=True,
        check=False,
    )
    stdout = str(completed.stdout or "").strip()
    try:
        parsed = json.loads(stdout) if stdout else {}
    except Exception:
        parsed = {"raw_stdout": stdout}
    return {
        "returncode": int(completed.returncode),
        "stdout": parsed,
        "stderr": str(completed.stderr or "").strip(),
        "executed_command": command,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan bounded worker delegation without giving away Codex-owned judgment.")
    parser.add_argument("--instruction", required=True)
    parser.add_argument("--risk-level", choices=["low", "medium", "high"], default="low")
    parser.add_argument("--task-profile", default="auto")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--input-ref", action="append", default=[])
    parser.add_argument("--output-expectation", default="")
    parser.add_argument("--force-provider", choices=["", "qwen_local", "deepseek"], default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--run", action="store_true", help="Execute the generated delegate command when safe.")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()

    payload = build_plan(
        instruction=str(args.instruction).strip(),
        risk_level=str(args.risk_level).strip().lower(),
        task_profile=str(args.task_profile).strip().lower(),
        task_id=str(args.task_id).strip(),
        input_refs=[str(item).strip() for item in list(args.input_ref or []) if str(item).strip()],
        output_expectation=str(args.output_expectation),
        provider=str(args.force_provider).strip(),
        output_path=str(args.output).strip(),
    )
    if args.run and payload.get("can_delegate"):
        payload["execution"] = _run_delegate(str(payload.get("delegate_command", "")).strip())
    if not args.no_write:
        payload["report_path"] = str(_write_report(payload))
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    if args.run and payload.get("execution", {}).get("returncode", 0) != 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
