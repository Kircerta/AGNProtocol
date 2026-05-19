#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from event_sourcing import load_events, load_checkpoint
from research_flow import run_research_unit

REPORT_PATH = ROOT / "reports" / "research_permissive_validation.json"


def _check(condition: bool, failures: list[str], code: str) -> None:
    if not condition:
        failures.append(code)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run permissive validation for AGN research flow")
    parser.add_argument("--transport", default=os.getenv("AGN_RESEARCH_WORKER_TRANSPORT", "stub"))
    args = parser.parse_args()
    os.environ["AGN_RESEARCH_WORKER_TRANSPORT"] = str(args.transport or "stub").strip().lower() or "stub"

    task_id = f"research-validation-{uuid4().hex[:8]}"
    summary = run_research_unit(task_id=task_id, unit_date=datetime.now(tz=timezone.utc).date().isoformat(), scenario="validation", max_steps=24)
    checkpoint = load_checkpoint(task_id) or {}
    trace_id = str(checkpoint.get("trace_id", "")).strip()
    events = load_events(trace_id)

    message_events = [event for event in events if str(event.get("event_type", "")).strip() == "RESEARCH_MESSAGE"]
    role_init_packets = [
        event for event in message_events
        if str((event.get("payload") or {}).get("kind", "")).strip() == "role_init_packet"
    ]
    role_init_acks = [
        event for event in message_events
        if str((event.get("payload") or {}).get("kind", "")).strip() == "role_init"
    ]
    rejections = [event for event in events if str(event.get("event_type", "")).strip() == "RESEARCH_ROUND_REJECTED"]
    forced = [event for event in events if str(event.get("event_type", "")).strip() == "RESEARCH_FORCED_DECISION"]
    degradations = [event for event in events if str(event.get("event_type", "")).strip() == "RESEARCH_DEGRADE_APPLIED"]
    archive = [event for event in events if str(event.get("event_type", "")).strip() == "RESEARCH_ARCHIVED"]
    final_review = checkpoint.get("final_review")
    failures: list[str] = []

    _check(str(summary.get("research_phase", "")).strip() == "done", failures, "unit_not_completed")
    _check(str(summary.get("state", "")).strip() == "DELIVERED", failures, "state_not_delivered")
    _check(bool(str(summary.get("archive_ref", "")).strip().startswith("agn://")), failures, "archive_missing")
    _check(int(summary.get("message_count", 0) or 0) >= 7, failures, "raw_messages_missing")
    _check(len(role_init_packets) >= 3, failures, "role_init_packets_missing")
    _check(len(role_init_acks) >= 3, failures, "role_init_acks_missing")
    _check(len(rejections) >= 1, failures, "rejection_path_missing")
    _check(len(forced) >= 1, failures, "third_round_forced_decision_missing")
    _check(len(degradations) >= 1, failures, "degradation_path_missing")
    _check(len(archive) >= 1, failures, "archive_event_missing")
    _check(isinstance(final_review, dict) and str(final_review.get("decision", "")).strip() == "yes", failures, "final_review_not_yes")
    _check(int(summary.get("max_packet_chars", 0) or 0) <= 2200, failures, "packet_too_large")

    report = {
        "ok": not failures,
        "task_id": task_id,
        "trace_id": trace_id,
        "summary": summary,
        "checks": {
            "raw_message_events": len(message_events),
            "role_init_packets": len(role_init_packets),
            "role_init_acks": len(role_init_acks),
            "rejection_events": len(rejections),
            "forced_decision_events": len(forced),
            "degradation_events": len(degradations),
            "archive_events": len(archive),
            "event_count": len(events),
        },
        "failures": failures,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
