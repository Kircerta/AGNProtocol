#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from agn_governed_execution import dispatch_provider_task, dispatch_review_request
from model_router import build_route_decision
from provider_registry import probe_capabilities


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("json_input_must_be_object")
    return payload


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, indent=2))


def cmd_status(_: argparse.Namespace) -> int:
    capabilities = probe_capabilities()
    reviewers = capabilities.get("reviewers", {})
    qwen = reviewers.get("qwen_local", {})
    deepseek = reviewers.get("deepseek", {})
    gemini = reviewers.get("gemini", {})
    claude = reviewers.get("claude", {})
    payload = {
        "ok": True,
        "qwen_local": {
            "available": bool(qwen.get("available")),
            "storage_ready": bool(qwen.get("storage_ready", True)),
            "model_path_exists": bool(qwen.get("model_path_exists", True)),
            "unavailable_reason": str(qwen.get("unavailable_reason", "")),
        },
        "deepseek": {
            "available": bool(deepseek.get("available")),
            "unavailable_reason": str(deepseek.get("unavailable_reason", "")),
        },
        "gemini": {
            "available": bool(gemini.get("available")),
            "path": str(gemini.get("path", "")),
        },
        "claude": {
            "available": bool(claude.get("available")),
            "path": str(claude.get("path", "")),
        },
        "guidance": [
            "Use qwen_local first for bounded low-risk transforms when available.",
            "If qwen_local is on hold because external storage or model storage is unavailable, fall back to deepseek for the same class of tasks.",
            "Use gemini flash as the preferred Gemini lane for lighter bounded remote tasks; reserve gemini pro for harder reasoning and review.",
            "Use review mode for high-tier Claude/Gemini review on real local files.",
        ],
    }
    _print(payload)
    return 0


def cmd_route(args: argparse.Namespace) -> int:
    payload = _load_json(Path(args.from_json_file))
    decision = build_route_decision(payload)
    _print(decision)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    payload = _load_json(Path(args.from_json_file))
    output = str(Path(args.output).resolve()) if args.output else ""
    routed = dispatch_provider_task(
        payload,
        caller="agent_collaboration",
        task_id=str(payload.get("task_id", "")).strip(),
        trace_id=str(payload.get("trace_id", "")).strip(),
        intent=str(payload.get("instruction", "")).strip() or "route_provider_task",
        reason="agent collaboration worker execution",
        risk_level=str(payload.get("risk", payload.get("risk_level", "medium"))).strip().lower() or "medium",
        output_dir=output,
        forced_provider=str(args.force_provider or "").strip().lower(),
    )
    envelope = dict(routed.get("envelope", {}))
    if routed.get("dispatch_meta"):
        envelope["dispatch_meta"] = routed["dispatch_meta"]
    if not routed.get("ok"):
        envelope = {
            "ok": False,
            "error": str(routed.get("error", "provider_dispatch_failed")),
            "failure_class": str(routed.get("failure_class", "")),
            "dispatch_meta": routed.get("dispatch_meta", {}),
        }
    _print(envelope)
    return 0 if envelope.get("ok") else 1


def cmd_review(args: argparse.Namespace) -> int:
    target = Path(args.file)
    if not target.is_absolute():
        target = (ROOT / target).resolve()
    include_dir = Path(args.include_dir)
    if not include_dir.is_absolute():
        include_dir = (ROOT / include_dir).resolve()
    payload = dispatch_review_request(
        file_path=str(target),
        include_dir=str(include_dir),
        review_goal=str(args.goal),
        extra_context=str(args.extra_context),
        claude_model=str(args.claude_model).strip(),
        gemini_model=str(args.gemini_model).strip(),
        excerpt_chars=max(1000, int(args.excerpt_chars)),
        timeout_sec=max(30.0, float(args.timeout_sec)),
        max_rounds=min(2, max(1, int(args.max_rounds))),
        caller="agent_collaboration",
        task_id=f"review-{target.stem}",
        trace_id=f"trace-review-{target.stem}",
        intent="run_flagship_review",
        reason="agent collaboration review execution",
        risk_level="medium",
    )
    if not payload.get("ok"):
        _print({"ok": False, "error": str(payload.get("error", "review_dispatch_failed")), "dispatch_meta": payload.get("dispatch_meta", {})})
        return 1
    report = payload["review_report"]
    _print({"ok": True, "report_path": report["report_path"], "run_id": report.get("run_id", ""), "status": report.get("status", ""), "dispatch_meta": payload.get("dispatch_meta", {})})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unified collaboration entrypoint for routed worker tasks and high-tier review")
    sub = parser.add_subparsers(dest="command", required=True)

    status_parser = sub.add_parser("status", help="Show provider readiness and fallback guidance")
    status_parser.set_defaults(func=cmd_status)

    route_parser = sub.add_parser("route", help="Show the routing decision for a bounded task JSON")
    route_parser.add_argument("--from-json-file", required=True)
    route_parser.set_defaults(func=cmd_route)

    run_parser = sub.add_parser("run", help="Route and execute a bounded task JSON")
    run_parser.add_argument("--from-json-file", required=True)
    run_parser.add_argument("--output", default="")
    run_parser.add_argument("--force-provider", choices=["qwen_local", "deepseek", "gemini", "claude"], default="")
    run_parser.set_defaults(func=cmd_run)

    review_parser = sub.add_parser("review", help="Run parallel flagship review against a real local file")
    review_parser.add_argument("--file", required=True)
    review_parser.add_argument("--include-dir", default=str(ROOT))
    review_parser.add_argument("--goal", default="Review this file for correctness, safety, and operational fragility.")
    review_parser.add_argument("--extra-context", default="")
    review_parser.add_argument("--claude-model", default="opus")
    review_parser.add_argument("--gemini-model", default="pro")
    review_parser.add_argument("--excerpt-chars", type=int, default=4000)
    review_parser.add_argument("--timeout-sec", type=float, default=600.0)
    review_parser.add_argument("--max-rounds", type=int, default=1)
    review_parser.set_defaults(func=cmd_review)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
