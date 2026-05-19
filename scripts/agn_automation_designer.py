#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "reports" / "automation_designer"

try:
    from capability_snapshot import build_capability_snapshot
except ImportError:  # pragma: no cover
    from scripts.capability_snapshot import build_capability_snapshot


FORBIDDEN_TOKENS = (
    "architecture",
    "governance",
    "final decision",
    "approve deployment",
    "security signoff",
    "destructive",
    "one-off",
    "single run only",
)


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _safe_slug(text: str, *, default: str, max_len: int = 56) -> str:
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


def infer_candidate_signals(task_summary: str) -> dict[str, bool]:
    text = str(task_summary or "").lower()
    return {
        "recurring": any(token in text for token in ("recurring", "repeat", "regularly", "each", "every ", "weekly", "hourly", "monitor", "check", "report", "digest", "refresh")),
        "monitoring": any(token in text for token in ("monitor", "watch", "check", "health", "status", "incident")),
        "reporting": any(token in text for token in ("report", "digest", "summary", "brief", "rollup")),
        "maintenance": any(token in text for token in ("refresh", "cleanup", "sync", "maintenance", "rotate", "backup")),
        "gui_or_visual": any(token in text for token in ("screenshot", "gui", "desktop", "window", "vision", "screen")),
    }


def automation_blockers(task_summary: str) -> list[str]:
    lowered = str(task_summary or "").lower()
    blockers = []
    for token in FORBIDDEN_TOKENS:
        if token in lowered:
            blockers.append(f"task mentions `{token}`, which should stay under direct human or Codex judgment.")
    return blockers


def classify_task(task_summary: str) -> str:
    signals = infer_candidate_signals(task_summary)
    if signals["monitoring"]:
        return "monitoring"
    if signals["reporting"]:
        return "reporting"
    if signals["maintenance"]:
        return "maintenance"
    return "general"


def schedule_for(*, classification: str, cadence: str, interval_hours: int, weekday: str, time_hhmm: str) -> dict[str, str]:
    clean_weekday = str(weekday).strip().upper() or "MON"
    hour_text, minute_text = (str(time_hhmm).strip() or "09:00").split(":", 1)
    hour = max(0, min(23, int(hour_text)))
    minute = max(0, min(59, int(minute_text)))
    clean_cadence = str(cadence).strip().lower()
    if clean_cadence == "auto":
        clean_cadence = "hourly" if classification == "monitoring" else "weekly"
    if clean_cadence == "hourly":
        interval = max(1, min(24, int(interval_hours)))
        return {
            "rrule": f"FREQ=HOURLY;INTERVAL={interval}",
            "human_schedule": f"Every {interval} hour(s)",
        }
    return {
        "rrule": f"FREQ=WEEKLY;BYDAY={clean_weekday};BYHOUR={hour};BYMINUTE={minute}",
        "human_schedule": f"Weekly on {clean_weekday} at {hour:02d}:{minute:02d}",
    }


def recommend_skills(task_summary: str, capability: dict[str, Any]) -> list[str]:
    installed = set(capability.get("skills", {}).get("installed", []))
    text = str(task_summary or "").lower()
    candidates = []
    if any(token in text for token in ("gui", "screenshot", "vision", "desktop")):
        candidates.extend(["agn-visual-operator", "agn-desktop-recovery", "agn-artifact-bridge"])
    if any(token in text for token in ("capability", "status", "preflight", "surface")):
        candidates.append("agn-capability-rhythm")
    if any(token in text for token in ("drift", "refresh", "protocol", "skill change", "workflow change")):
        candidates.extend(["agn-memory-refresh", "agn-memory-ingestion"])
    if any(token in text for token in ("review", "approval", "risk")):
        candidates.append("agn-review-gate")
    deduped = []
    seen: set[str] = set()
    for name in candidates:
        if name in seen or name not in installed:
            continue
        seen.add(name)
        deduped.append(name)
    return deduped


def skill_links(skill_names: list[str]) -> list[str]:
    links = []
    for name in skill_names:
        path = Path.home() / ".codex_agn" / "skills" / name / "SKILL.md"
        links.append(f"[$%s](%s)" % (name, str(path)))
    return links


def build_name(task_summary: str, classification: str) -> str:
    text = str(task_summary or "").strip()
    if not text:
        return "AGN automation"
    stopwords = {"and", "or", "the", "a", "an", "to", "of", "for", "with", "every"}
    words = [word for word in "".join(ch if ch.isalnum() else " " for ch in text).split() if word and word.lower() not in stopwords]
    label = " ".join(words[:4]).strip()
    if not label:
        label = classification
    return label[:48]


def build_prompt(*, task_summary: str, deliverable: str, gating_rules: list[str], linked_skills: list[str]) -> str:
    sentences = [str(task_summary).strip().rstrip(".") + "."]
    if deliverable:
        sentences.append(f"Write the output to {deliverable}.")
    if gating_rules:
        sentences.extend(f"Only run if {rule}." for rule in gating_rules if str(rule).strip())
    if linked_skills:
        sentences.append("Use these skills when they fit: " + ", ".join(linked_skills) + ".")
    return " ".join(part.strip() for part in sentences if part.strip())


def build_payload(
    *,
    task_summary: str,
    cadence: str,
    interval_hours: int,
    weekday: str,
    time_hhmm: str,
    workspaces: list[str],
    deliverable: str,
    gating_rules: list[str],
    status: str,
) -> dict[str, Any]:
    signals = infer_candidate_signals(task_summary)
    blockers = automation_blockers(task_summary)
    classification = classify_task(task_summary)
    capability = build_capability_snapshot()
    candidate = bool(signals["recurring"] or classification in {"monitoring", "reporting", "maintenance"}) and not blockers
    schedule = schedule_for(
        classification=classification,
        cadence=cadence,
        interval_hours=interval_hours,
        weekday=weekday,
        time_hhmm=time_hhmm,
    )
    recommended = recommend_skills(task_summary, capability)
    linked_skills = skill_links(recommended)
    automation_name = build_name(task_summary, classification)
    prompt = build_prompt(
        task_summary=task_summary,
        deliverable=deliverable,
        gating_rules=gating_rules,
        linked_skills=linked_skills,
    )
    return {
        "ok": True,
        "generated_at": utc_now_iso(),
        "task_summary": task_summary,
        "classification": classification,
        "signals": signals,
        "blockers": blockers,
        "automation_candidate": candidate,
        "recommended_skills": recommended,
        "automation_spec": {
            "name": automation_name,
            "prompt": prompt,
            "rrule": schedule["rrule"],
            "human_schedule": schedule["human_schedule"],
            "cwds": workspaces,
            "status": status,
        },
        "notes": [
            "This helper proposes automation but does not modify automation files.",
            "Use hourly or weekly schedules so the proposal stays compatible with the current UI constraints.",
            "Automation is appropriate for recurring monitoring, reports, or maintenance, not for final judgment or destructive authority.",
        ],
    }


SCHEDULER_JOBS_PATH = ROOT / "agn2" / "awakening" / "scheduler_jobs.json"


def _rrule_to_interval_seconds(rrule: str, interval_hours: int) -> int:
    """Convert an RRULE hint into scheduler interval_seconds."""
    rrule_lower = str(rrule or "").lower()
    if "hourly" in rrule_lower:
        return max(900, interval_hours * 3600)
    if "weekly" in rrule_lower:
        return 7 * 24 * 3600
    if "daily" in rrule_lower:
        return 24 * 3600
    return max(3600, interval_hours * 3600)


def _apply_to_scheduler(payload: dict[str, Any]) -> dict[str, Any]:
    """Append a validated automation proposal to scheduler_jobs.json.

    Returns a dict describing what happened. Only applies if the proposal
    is an automation_candidate and the job name doesn't already exist.
    """
    if not payload.get("automation_candidate", False):
        return {"ok": False, "reason": "not_an_automation_candidate"}

    spec = payload.get("automation_spec", {})
    job_name = _safe_slug(str(spec.get("name", "")), default="auto-job")
    prompt = str(spec.get("prompt", "")).strip()
    if not prompt:
        return {"ok": False, "reason": "empty_automation_prompt"}

    # Load existing scheduler jobs
    try:
        scheduler = json.loads(SCHEDULER_JOBS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "reason": "scheduler_jobs_not_readable"}

    jobs = scheduler.get("jobs", [])
    existing_names = {str(j.get("name", "")).strip() for j in jobs}
    if job_name in existing_names:
        return {"ok": False, "reason": f"job_already_exists:{job_name}"}

    rrule = str(spec.get("rrule", "")).strip()
    interval_hours = max(1, int(payload.get("signals", {}).get("interval_hours", 4) or 4))
    cwds = spec.get("cwds", [])

    new_job = {
        "name": job_name,
        "description": str(payload.get("task_summary", "Automated task")).strip()[:200],
        "command": [".venv/bin/python", "scripts/agn_automation_designer.py", "--task-summary", prompt, "--no-write"],
        "interval_seconds": _rrule_to_interval_seconds(rrule, interval_hours),
        "timeout_seconds": 120,
        "enabled": str(spec.get("status", "PAUSED")).strip().upper() == "ACTIVE",
        "destructive": False,
        "source": "automation_designer",
        "created_at": utc_now_iso(),
    }
    if cwds:
        new_job["cwds"] = cwds

    jobs.append(new_job)
    scheduler["jobs"] = jobs
    _atomic_write_json(SCHEDULER_JOBS_PATH, scheduler)

    return {
        "ok": True,
        "job_name": job_name,
        "enabled": new_job["enabled"],
        "interval_seconds": new_job["interval_seconds"],
        "scheduler_path": str(SCHEDULER_JOBS_PATH),
    }


def _write_report(payload: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = _safe_slug(str(payload.get("task_summary", "")), default="automation")
    path = REPORT_DIR / f"{timestamp}-{slug}.json"
    latest = REPORT_DIR / "latest.json"
    _atomic_write_json(path, payload)
    _atomic_write_json(latest, payload)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Decide whether a task should become a recurring automation and emit a structured proposal.")
    parser.add_argument("--task-summary", required=True)
    parser.add_argument("--cadence", choices=["auto", "hourly", "weekly"], default="auto")
    parser.add_argument("--interval-hours", type=int, default=4)
    parser.add_argument("--weekday", default="MON")
    parser.add_argument("--time", default="09:00")
    parser.add_argument("--workspace", action="append", default=[])
    parser.add_argument("--deliverable", default="")
    parser.add_argument("--gating-rule", action="append", default=[])
    parser.add_argument("--status", choices=["ACTIVE", "PAUSED"], default="ACTIVE")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the proposal to the scheduler by appending to scheduler_jobs.json (only if automation_candidate=true).",
    )
    args = parser.parse_args()

    payload = build_payload(
        task_summary=str(args.task_summary).strip(),
        cadence=str(args.cadence).strip().lower(),
        interval_hours=max(1, int(args.interval_hours)),
        weekday=str(args.weekday).strip(),
        time_hhmm=str(args.time).strip(),
        workspaces=[str(item).strip() for item in list(args.workspace or []) if str(item).strip()],
        deliverable=str(args.deliverable).strip(),
        gating_rules=[str(item).strip() for item in list(args.gating_rule or []) if str(item).strip()],
        status=str(args.status).strip().upper(),
    )
    if not args.no_write:
        payload["report_path"] = str(_write_report(payload))

    if args.apply:
        applied = _apply_to_scheduler(payload)
        payload["applied_to_scheduler"] = applied

    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
