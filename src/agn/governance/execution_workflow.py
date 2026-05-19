"""AGN execution workflow helpers.

This is the real package implementation for AGN's task preflight, Ghostty
workspace setup, bounded delegation, and flagship review helper surface. The
legacy script remains only as a CLI compatibility shim.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]

from agn.governance.operator_brief import build_operator_brief
from agn.governance.task_start_kernel import build_task_start_kernel


PACKAGE_PATH = "agn.governance.execution_workflow"
LEGACY_SCRIPT_SHIM = "scripts/agn2_execution_workflow.py"
PREFLIGHT_DIR = ROOT / "runtime" / "admin_control" / "preflight"
CONTROL_PLANE_APP = ROOT / "agn2" / "control_plane" / "src-tauri" / "target" / "release" / "bundle" / "macos" / "AGN2.0 Control Plane.app"
CONVERSATION_MONITOR_APP = ROOT / "agn2" / "conversation_monitor" / "src-tauri" / "target" / "release" / "bundle" / "macos" / "AGN Conversation Monitor.app"
try:
    from agn.core.desktop_provider import get_desktop_control_bin
    GUI_AGENT_BIN = get_desktop_control_bin()
except ImportError:  # pragma: no cover
    GUI_AGENT_BIN = Path.home() / ".codex" / "bin" / "gui-agent"
WORKER_PROFILES = {
    "structured_transform",
    "json_extraction",
    "label_normalization",
    "ocr_cleanup",
    "batch_cleaning",
    "bounded_summarization",
    "general_analysis",
}
RISK_LEVELS = {"low", "medium", "high"}


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _load_json(path: Path, default: Any) -> Any:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return payload


def _safe_slug(text: str, *, default: str) -> str:
    safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(text or "").strip())
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe.strip("-") or default


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, indent=2))


def _provider_summary() -> dict[str, Any]:
    payload = _load_json(ROOT / "runtime" / "provider_capabilities.json", {})
    if not isinstance(payload, dict):
        return {}
    reviewers = payload.get("reviewers", {})
    if not isinstance(reviewers, dict):
        reviewers = {}
    return {
        "qwen_local": bool((reviewers.get("qwen_local") or {}).get("available")),
        "deepseek": bool((reviewers.get("deepseek") or {}).get("available")),
        "gemini": bool((reviewers.get("gemini") or {}).get("available")),
        "claude": bool((reviewers.get("claude") or {}).get("available")),
    }


def system_snapshot() -> dict[str, Any]:
    lifecycle = _load_json(ROOT / "runtime" / "admin_control" / "lifecycle" / "agn2_system.json", {})
    system_mode = _load_json(ROOT / "runtime" / "admin_control" / "system_mode.json", {})
    overview = _load_json(ROOT / "runtime" / "admin_control" / "read_models" / "overview.json", {})
    return {
        "captured_at": _utc_now_iso(),
        "lifecycle": lifecycle if isinstance(lifecycle, dict) else {},
        "system_mode": system_mode if isinstance(system_mode, dict) else {},
        "overview": overview if isinstance(overview, dict) else {},
        "provider_summary": _provider_summary(),
        "control_plane_app_exists": CONTROL_PLANE_APP.exists(),
        "conversation_monitor_app_exists": CONVERSATION_MONITOR_APP.exists(),
        "gui_agent_exists": GUI_AGENT_BIN.exists(),
        "ghostty_available": bool(shutil.which("ghostty")),
    }


def build_preflight_payload(
    *,
    task_summary: str,
    risk_level: str,
    task_id: str,
    trace_id: str,
    subsystem: str,
    needs_control_plane: bool,
    needs_desktop: bool,
    needs_history: bool,
    needs_worker: bool,
    needs_review: bool,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if risk_level not in RISK_LEVELS:
        raise ValueError(f"invalid_risk_level:{risk_level}")
    snapshot_payload = snapshot or system_snapshot()
    system_mode = snapshot_payload.get("system_mode", {}) if isinstance(snapshot_payload.get("system_mode"), dict) else {}
    lifecycle = snapshot_payload.get("lifecycle", {}) if isinstance(snapshot_payload.get("lifecycle"), dict) else {}
    provider_summary = snapshot_payload.get("provider_summary", {}) if isinstance(snapshot_payload.get("provider_summary"), dict) else {}
    task_start_kernel = build_task_start_kernel(
        task_summary=task_summary,
        risk_level=risk_level,
        snapshot=snapshot_payload,
        needs_desktop=needs_desktop,
        refresh_host_info=True,
    )
    provider_summary = (
        task_start_kernel.get("runtime_snapshot", {}).get("provider_summary", {})
        if isinstance(task_start_kernel.get("runtime_snapshot", {}), dict)
        else {}
    )
    memory_recall = task_start_kernel.get("memory_recall", {}) if isinstance(task_start_kernel.get("memory_recall"), dict) else {}
    host_info = task_start_kernel.get("host_info", {}) if isinstance(task_start_kernel.get("host_info"), dict) else {}
    recommended_surfaces: list[dict[str, str]] = [
        {
            "surface": "agn2_system",
            "reason": "Start from the canonical lifecycle and mode snapshot instead of local assumptions.",
            "entry": "python3 scripts/agn2_system.py status",
        }
    ]
    if needs_control_plane or risk_level in {"medium", "high"}:
        recommended_surfaces.append(
            {
                "surface": "control_plane",
                "reason": "Use the formal human control surface for governed visibility and gate state.",
                "entry": f"open {shlex.quote(str(CONTROL_PLANE_APP))}",
            }
        )
    if needs_desktop:
        recommended_surfaces.extend(
            [
                {
                    "surface": "ghostty_workspace",
                    "reason": "Use Ghostty as the primary terminal object layer, not a plain shell habit.",
                    "entry": "python3 scripts/agn2_execution_workflow.py ghostty-workspace",
                },
                {
                    "surface": "desktop_adapter",
                    "reason": "Use governed desktop observation and gated terminal actions instead of ad hoc GUI control.",
                    "entry": "python3 scripts/desktop_adapter.py",
                },
            ]
        )
    if needs_history:
        recommended_surfaces.append(
            {
                "surface": "conversation_monitor",
                "reason": "Inspect internal AGN language-layer evidence instead of relying on summaries alone.",
                "entry": f"open {shlex.quote(str(CONVERSATION_MONITOR_APP))}",
            }
        )
    if needs_worker:
        recommended_surfaces.append(
            {
                "surface": "worker_delegate",
                "reason": "Offload bounded low-risk transforms to worker-grade models and keep Codex focused on judgment.",
                "entry": "python3 scripts/agn2_execution_workflow.py delegate --instruction \"...\"",
            }
        )
    if needs_review or risk_level in {"medium", "high"}:
        recommended_surfaces.append(
            {
                "surface": "flagship_review",
                "reason": "Bring in Gemini or Claude review for architecture, ambiguity, or high-risk verification.",
                "entry": "python3 scripts/agn2_execution_workflow.py review --file <path>",
            }
        )
    regression_signals = [
        "Starting directly in a plain shell without checking AGN2.0 lifecycle and mode.",
        "Doing all repo scanning or structured cleanup manually instead of delegating worker-grade labor.",
        "Skipping Ghostty, control-plane, or desktop surfaces when the task is better observed than inferred.",
        "Treating low-tier worker output as final judgment.",
        "Hiding key actions in ad hoc terminal steps without leaving inspectable evidence.",
    ]
    execution_checks = [
        {
            "check": "authority_model",
            "required": True,
            "status": "ok",
            "detail": "The operator remains the final authority; execution units follow governed controls.",
        },
        {
            "check": "system_mode",
            "required": True,
            "status": "blocked" if str(system_mode.get("mode", "")).strip().lower() == "emergency_stop" else "ok",
            "detail": f"Current system mode: {system_mode.get('mode', '') or 'unknown'}",
        },
        {
            "check": "lifecycle_state",
            "required": True,
            "status": "ok" if str(lifecycle.get('status', '')).strip().lower() == "running" else "attention",
            "detail": f"Lifecycle status: {lifecycle.get('status', '') or 'unknown'}",
        },
        {
            "check": "worker_providers_available",
            "required": needs_worker,
            "status": (
                "ok" if any(bool(provider_summary.get(p)) for p in ("qwen_local", "deepseek"))
                else ("attention" if needs_worker else "n/a")
            ),
            "detail": (
                f"Worker-grade providers: qwen_local={'✓' if provider_summary.get('qwen_local') else '✗'}, "
                f"deepseek={'✓' if provider_summary.get('deepseek') else '✗'}. "
                "At least one worker-grade provider is needed for delegation."
            ),
        },
        {
            "check": "reviewer_providers_available",
            "required": needs_review or risk_level in {"medium", "high"},
            "status": (
                "ok" if any(bool(provider_summary.get(p)) for p in ("gemini", "claude"))
                else ("attention" if (needs_review or risk_level in {"medium", "high"}) else "n/a")
            ),
            "detail": (
                f"Reviewer-grade providers: gemini={'✓' if provider_summary.get('gemini') else '✗'}, "
                f"claude={'✓' if provider_summary.get('claude') else '✗'}. "
                "At least one reviewer-grade provider is needed for flagship review."
            ),
        },
        {
            "check": "worker_plan",
            "required": needs_worker,
            "status": "ok" if needs_worker else "n/a",
            "detail": "Decide bounded tasks that can be sent to Qwen or DeepSeek before writing them by hand.",
        },
        {
            "check": "review_plan",
            "required": needs_review or risk_level in {"medium", "high"},
            "status": "ok" if (needs_review or risk_level in {"medium", "high"}) else "n/a",
            "detail": "Use Gemini Pro or Claude review for ambiguity, high-risk work, or architectural verification.",
        },
        {
            "check": "desktop_surface",
            "required": needs_desktop,
            "status": "ok" if needs_desktop else "n/a",
            "detail": "Prefer Ghostty, control plane, screenshots, or desktop adapters when they fit the task better than a plain terminal.",
        },
        {
            "check": "memory_recall",
            "required": True,
            "status": "ok" if bool(memory_recall.get("ok")) else "attention",
            "detail": (
                f"Task-start recall consulted: {len(memory_recall.get('priors', [])) if isinstance(memory_recall.get('priors'), list) else 0} priors, "
                f"{len(memory_recall.get('tool_reality_cards', [])) if isinstance(memory_recall.get('tool_reality_cards'), list) else 0} tool reality cards. "
                "Runtime facts remain authoritative; memory priors are advisory."
            ),
        },
        {
            "check": "host_info",
            "required": True,
            "status": "ok" if str(host_info.get("task_readiness", {}).get("status", "")).strip() == "ready" else "attention",
            "detail": str(host_info.get("task_readiness", {}).get("summary", "")).strip(),
        },
    ]
    operator_brief = build_operator_brief(
        task_summary=task_summary,
        risk_level=risk_level,
        system_snapshot=snapshot_payload,
        execution_checks=execution_checks,
        task_start_kernel=task_start_kernel,
        recommended_surfaces=recommended_surfaces,
    )
    return {
        "ok": True,
        "generated_at": _utc_now_iso(),
        "task_summary": task_summary,
        "task_id": task_id,
        "trace_id": trace_id,
        "subsystem": subsystem,
        "risk_level": risk_level,
        "identity": {
            "admin": "Operator",
            "controller": "AGN 2.0 Codex - Central Execution Unit",
        },
        "system_snapshot": snapshot_payload,
        "recommended_surfaces": recommended_surfaces,
        "execution_checks": execution_checks,
        "worker_and_review_state": {
            "qwen_local": bool(provider_summary.get("qwen_local")),
            "deepseek": bool(provider_summary.get("deepseek")),
            "gemini": bool(provider_summary.get("gemini")),
            "claude": bool(provider_summary.get("claude")),
        },
        "task_start_kernel": task_start_kernel,
        "memory_recall": memory_recall,
        "host_info": host_info,
        "operator_brief": operator_brief,
        "next_actions": [
            "Confirm the best execution surface before opening a plain shell.",
            "Use HOST_INFO.md or host_info.json as the local hardware and dependency truth before assuming capabilities.",
            "Explicitly decide which bounded labor will be delegated before coding it manually.",
            "Keep auditability intact: prefer governed surfaces and inspectable outputs.",
        ],
        "regression_signals": regression_signals,
    }


def write_preflight(payload: dict[str, Any]) -> Path:
    PREFLIGHT_DIR.mkdir(parents=True, exist_ok=True)
    slug = _safe_slug(str(payload.get("task_summary", "")), default="task")
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = PREFLIGHT_DIR / f"{timestamp}-{slug}.json"
    latest = PREFLIGHT_DIR / "latest.json"
    text = json.dumps(payload, ensure_ascii=True, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    history = sorted(candidate for candidate in PREFLIGHT_DIR.glob("*.json") if candidate.name != "latest.json")
    if len(history) > 20:
        for candidate in history[:-20]:
            candidate.unlink(missing_ok=True)
    return path


def _command_preview(cmd: list[str]) -> str:
    return shlex.join(cmd)


def _seed_text(name: str) -> str:
    seeds = {
        "status": "clear\npython3 scripts/agn2_system.py status\n",
        "implementation": "clear\npwd\n",
        "validation": "clear\ngit status --short\n",
        "review": "clear\npython3 scripts/agent_collaboration.py status\n",
    }
    return seeds.get(name, "")


def build_ghostty_workspace_commands(*, cwd: Path, execute: bool, plain_shells: bool) -> list[list[str]]:
    action_flag = "--execute" if execute else "--dry-run"
    tabs = [
        ("new-window", "status"),
        ("new-tab", "implementation"),
        ("new-tab", "validation"),
        ("new-tab", "review"),
    ]
    commands: list[list[str]] = []
    for action, name in tabs:
        cmd = [str(GUI_AGENT_BIN), "ghostty", action, action_flag, "--cwd", str(cwd)]
        seed = _seed_text(name)
        if seed and not plain_shells:
            cmd.extend(["--input", seed])
        commands.append(cmd)
    return commands


def _run_cli_json(cmd: list[str], *, cwd: Path) -> dict[str, Any]:
    completed = subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=False,
    )
    stdout = str(completed.stdout or "").strip()
    parsed: Any
    try:
        parsed = json.loads(stdout) if stdout else {}
    except Exception:
        parsed = {"raw_stdout": stdout}
    return {
        "returncode": int(completed.returncode),
        "stdout": parsed,
        "stderr": str(completed.stderr or "").strip(),
        "command": cmd,
    }


def build_delegate_request(
    *,
    instruction: str,
    task_profile: str,
    risk_level: str,
    input_refs: list[str],
    output_expectation: str,
    task_id: str,
) -> dict[str, Any]:
    worker_envelope = {
        "system_constraints": [
            "bounded_worker_task",
            "no_governance_judgment",
            "no_architecture_judgment",
            "no_final_review_authority",
        ],
        "user_instruction": instruction.strip(),
        "input_refs": input_refs,
        "output_expectation": output_expectation.strip(),
    }
    return {
        "task_id": task_id,
        "task_type": task_profile,
        "task_profile": task_profile,
        "prompt": "Bounded worker task for AGN2.0.\nTask envelope:\n" + json.dumps(worker_envelope, ensure_ascii=True, indent=2),
        "response_mode": "text",
        "risk_level": risk_level,
        "logical_complexity": "low" if risk_level == "low" else "medium",
        "verification_cost": "low",
        "cost_sensitivity": "high",
        "allow_fallback": True,
        "metadata": {
            "created_by": "agn2_execution_workflow",
            "worker_only": True,
            "input_refs": input_refs,
            "output_expectation": output_expectation.strip(),
        },
    }


def cmd_preflight(args: argparse.Namespace) -> int:
    payload = build_preflight_payload(
        task_summary=str(args.task_summary).strip(),
        risk_level=str(args.risk_level).strip().lower(),
        task_id=str(args.task_id).strip() or f"agn2-task-{_safe_slug(args.task_summary, default='task')}",
        trace_id=str(args.trace_id).strip() or f"trace-{_safe_slug(args.task_summary, default='task')}",
        subsystem=str(args.subsystem).strip() or "agn2",
        needs_control_plane=bool(args.needs_control_plane),
        needs_desktop=bool(args.needs_desktop),
        needs_history=bool(args.needs_history),
        needs_worker=bool(args.needs_worker),
        needs_review=bool(args.needs_review),
    )
    output_path = write_preflight(payload) if not args.no_write else None
    if output_path is not None:
        payload["saved_to"] = str(output_path)
    _print_json(payload)
    return 0


def cmd_ghostty_workspace(args: argparse.Namespace) -> int:
    if not GUI_AGENT_BIN.exists():
        _print_json({"ok": False, "error": f"gui_agent_missing:{GUI_AGENT_BIN}"})
        return 1
    cwd = Path(args.cwd).resolve()
    commands = build_ghostty_workspace_commands(cwd=cwd, execute=bool(args.execute), plain_shells=bool(args.plain_shells))
    results = [_run_cli_json(cmd, cwd=ROOT) for cmd in commands]
    ok = all(result["returncode"] == 0 for result in results)
    _print_json(
        {
            "ok": ok,
            "execute": bool(args.execute),
            "cwd": str(cwd),
            "plain_shells": bool(args.plain_shells),
            "commands": [_command_preview(item["command"]) for item in results],
            "results": results,
        }
    )
    return 0 if ok else 1


def cmd_delegate(args: argparse.Namespace) -> int:
    risk_level = str(args.risk_level).strip().lower()
    task_profile = str(args.task_profile).strip().lower()
    if risk_level not in {"low", "medium"}:
        _print_json({"ok": False, "error": "delegate_only_supports_low_or_medium_risk"})
        return 1
    if task_profile not in WORKER_PROFILES:
        _print_json({"ok": False, "error": f"unsupported_worker_profile:{task_profile}"})
        return 1
    task_id = str(args.task_id).strip() or f"delegate-{_safe_slug(args.instruction, default='task')}"
    request = build_delegate_request(
        instruction=str(args.instruction),
        task_profile=task_profile,
        risk_level=risk_level,
        input_refs=[str(item).strip() for item in list(args.input_ref or []) if str(item).strip()],
        output_expectation=str(args.output_expectation or ""),
        task_id=task_id,
    )
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
        request_path = Path(handle.name)
        json.dump(request, handle, ensure_ascii=True, indent=2)
        handle.write("\n")
    try:
        command = [
            sys.executable,
            str(ROOT / "scripts" / "agent_collaboration.py"),
            "route" if args.route_only else "run",
            "--from-json-file",
            str(request_path),
        ]
        if not args.route_only and str(args.force_provider).strip():
            command.extend(["--force-provider", str(args.force_provider).strip()])
        if not args.route_only and str(args.output).strip():
            command.extend(["--output", str(Path(args.output).resolve())])
        result = _run_cli_json(command, cwd=ROOT)
        result["request"] = request
        result["request_path"] = str(request_path)
        _print_json(result)
        return 0 if result["returncode"] == 0 else 1
    finally:
        request_path.unlink(missing_ok=True)


def cmd_review(args: argparse.Namespace) -> int:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "agent_collaboration.py"),
        "review",
        "--file",
        str(Path(args.file).resolve()),
        "--include-dir",
        str(Path(args.include_dir).resolve()),
        "--goal",
        str(args.goal),
        "--extra-context",
        str(args.extra_context),
        "--gemini-model",
        str(args.gemini_model),
        "--claude-model",
        str(args.claude_model),
        "--max-rounds",
        str(int(args.max_rounds)),
        "--timeout-sec",
        str(float(args.timeout_sec)),
    ]
    result = _run_cli_json(command, cwd=ROOT)
    _print_json(result)
    return 0 if result["returncode"] == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Externalized AGN2.0 task workflow helpers for preflight, Ghostty workspace setup, delegation, and review"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    preflight_parser = sub.add_parser("preflight", help="Generate and optionally persist a structured AGN2.0 task preflight")
    preflight_parser.add_argument("--task-summary", required=True)
    preflight_parser.add_argument("--risk-level", choices=sorted(RISK_LEVELS), default="medium")
    preflight_parser.add_argument("--task-id", default="")
    preflight_parser.add_argument("--trace-id", default="")
    preflight_parser.add_argument("--subsystem", default="agn2")
    preflight_parser.add_argument("--needs-control-plane", action="store_true")
    preflight_parser.add_argument("--needs-desktop", action="store_true")
    preflight_parser.add_argument("--needs-history", action="store_true")
    preflight_parser.add_argument("--needs-worker", action="store_true")
    preflight_parser.add_argument("--needs-review", action="store_true")
    preflight_parser.add_argument("--no-write", action="store_true")
    preflight_parser.set_defaults(func=cmd_preflight)

    workspace_parser = sub.add_parser("ghostty-workspace", help="Preview or launch the standard AGN2.0 Ghostty task workspace")
    workspace_parser.add_argument("--cwd", default=str(ROOT))
    workspace_parser.add_argument("--execute", action="store_true")
    workspace_parser.add_argument("--plain-shells", action="store_true")
    workspace_parser.set_defaults(func=cmd_ghostty_workspace)

    delegate_parser = sub.add_parser("delegate", help="Route a bounded worker-grade task through the collaboration runtime")
    delegate_parser.add_argument("--instruction", required=True)
    delegate_parser.add_argument("--task-profile", choices=sorted(WORKER_PROFILES), default="general_analysis")
    delegate_parser.add_argument("--risk-level", choices=["low", "medium"], default="low")
    delegate_parser.add_argument("--input-ref", action="append", default=[])
    delegate_parser.add_argument("--output-expectation", default="")
    delegate_parser.add_argument("--task-id", default="")
    delegate_parser.add_argument("--route-only", action="store_true")
    delegate_parser.add_argument("--output", default="")
    delegate_parser.add_argument("--force-provider", choices=["", "qwen_local", "deepseek", "gemini", "claude"], default="")
    delegate_parser.set_defaults(func=cmd_delegate)

    review_parser = sub.add_parser("review", help="Run flagship file review through the existing collaboration runtime")
    review_parser.add_argument("--file", required=True)
    review_parser.add_argument("--include-dir", default=str(ROOT))
    review_parser.add_argument(
        "--goal",
        default="Review this file for correctness, risk handling, and alignment with AGN2.0 operating constraints.",
    )
    review_parser.add_argument("--extra-context", default="")
    review_parser.add_argument("--gemini-model", default="pro")
    review_parser.add_argument("--claude-model", default="opus")
    review_parser.add_argument("--max-rounds", type=int, default=1)
    review_parser.add_argument("--timeout-sec", type=float, default=600.0)
    review_parser.set_defaults(func=cmd_review)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except (ValueError, FileNotFoundError) as exc:
        _print_json({"ok": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
