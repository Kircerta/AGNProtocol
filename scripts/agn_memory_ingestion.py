#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "reports" / "memory_ingestion"
CHANGE_KINDS = {"skill", "protocol", "workflow", "capability", "governance", "automation", "memory"}


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


def _default_kind(change_kind: str) -> str:
    mapping = {
        "skill": "decision",
        "protocol": "decision",
        "workflow": "decision",
        "capability": "status",
        "governance": "constraint",
        "automation": "todo",
        "memory": "status",
    }
    return mapping.get(str(change_kind).strip().lower(), "decision")


def build_manual_records(
    *,
    change_kind: str,
    name: str,
    summary: str,
    operating_impact: str,
    source_refs: list[str],
    scope: str,
    related_task: str,
    author: str,
    confidence: str,
    constraints: list[str],
    follow_ups: list[str],
) -> list[dict[str, Any]]:
    clean_kind = str(change_kind).strip().lower()
    if clean_kind not in CHANGE_KINDS:
        raise ValueError(f"invalid_change_kind:{clean_kind}")
    records: list[dict[str, Any]] = [
        {
            "kind": _default_kind(clean_kind),
            "scope": scope,
            "summary": f"{clean_kind}:{name} -> {summary}",
            "fact_payload": {
                "change_kind": clean_kind,
                "name": name,
                "operating_impact": operating_impact,
                "source_refs": source_refs,
                "do_not_compress": True,
            },
            "source_refs": source_refs,
            "task_id": related_task,
            "author": author,
            "confidence": confidence,
        }
    ]
    for item in constraints:
        if not str(item).strip():
            continue
        records.append(
            {
                "kind": "constraint",
                "scope": scope,
                "summary": f"{clean_kind}:{name} constraint -> {str(item).strip()}",
                "fact_payload": {
                    "change_kind": clean_kind,
                    "name": name,
                    "constraint": str(item).strip(),
                    "do_not_compress": True,
                },
                "source_refs": source_refs,
                "task_id": related_task,
                "author": author,
                "confidence": confidence,
            }
        )
    for item in follow_ups:
        if not str(item).strip():
            continue
        records.append(
            {
                "kind": "todo",
                "scope": scope,
                "summary": f"{clean_kind}:{name} follow-up -> {str(item).strip()}",
                "fact_payload": {
                    "change_kind": clean_kind,
                    "name": name,
                    "follow_up": str(item).strip(),
                    "do_not_compress": True,
                },
                "source_refs": source_refs,
                "task_id": related_task,
                "author": author,
                "confidence": confidence,
            }
        )
    return records


def build_refresh_records(
    *,
    report_path: str,
    scope: str,
    author: str,
    confidence: str,
    related_task: str,
) -> list[dict[str, Any]]:
    path = Path(report_path).expanduser().resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    diff = payload.get("diff", {}) if isinstance(payload.get("diff"), dict) else {}
    changed_groups = [str(item).strip() for item in diff.get("changed_groups", []) if str(item).strip()]
    main_summary = (
        f"memory_refresh_ingested -> {int(diff.get('changed_count', 0))} changed files across "
        + (", ".join(changed_groups) if changed_groups else "no groups")
    )
    records: list[dict[str, Any]] = [
        {
            "kind": "status",
            "scope": scope,
            "summary": main_summary,
            "fact_payload": {
                "change_kind": "memory",
                "changed_groups": changed_groups,
                "changed_count": int(diff.get("changed_count", 0)),
                "added": diff.get("added", []),
                "removed": diff.get("removed", []),
                "modified": diff.get("modified", []),
                "report_path": str(path),
                "do_not_compress": True,
            },
            "source_refs": [str(path)],
            "task_id": related_task,
            "author": author,
            "confidence": confidence,
        }
    ]
    for action in payload.get("recommended_actions", []):
        if not str(action).strip():
            continue
        records.append(
            {
                "kind": "todo",
                "scope": scope,
                "summary": f"memory_refresh_follow_up -> {str(action).strip()}",
                "fact_payload": {
                    "change_kind": "memory",
                    "recommended_action": str(action).strip(),
                    "report_path": str(path),
                    "do_not_compress": True,
                },
                "source_refs": [str(path)],
                "task_id": related_task,
                "author": author,
                "confidence": confidence,
            }
        )
    return records


def dispatch_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    from dispatcher_runtime import dispatch_request  # type: ignore

    results: list[dict[str, Any]] = []
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d%H%M%S")
    for index, record in enumerate(records, start=1):
        payload = {
            "trace_id": f"trace-agn-memory-ingestion-{stamp}-{index}",
            "task_id": str(record.get("task_id", "")).strip() or f"agn-memory-ingestion-{stamp}-{index}",
            "caller": "codex",
            "target": "memory_recorder",
            "target_kind": "memory_recorder",
            "intent": "record_operating_memory",
            "reason": "Persist structured AGN operating memory without compaction drift.",
            "risk_level": "low",
            "input_payload": record,
            "input_refs": [str(item).strip() for item in record.get("source_refs", []) if str(item).strip()],
        }
        results.append(dispatch_request(payload))
    return results


def _write_report(payload: dict[str, Any], *, label: str) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORT_DIR / f"{timestamp}-{_safe_slug(label, default='memory-ingestion')}.json"
    latest = REPORT_DIR / "latest.json"
    _atomic_write_json(path, payload)
    _atomic_write_json(latest, payload)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Turn AGN skill, protocol, and workflow changes into append-only operating memory records.")
    sub = parser.add_subparsers(dest="command", required=True)

    record_parser = sub.add_parser("record", help="Create structured records for a new skill, protocol, or workflow change")
    record_parser.add_argument("--change-kind", choices=sorted(CHANGE_KINDS), required=True)
    record_parser.add_argument("--name", required=True)
    record_parser.add_argument("--summary", required=True)
    record_parser.add_argument("--operating-impact", default="")
    record_parser.add_argument("--source-ref", action="append", default=[])
    record_parser.add_argument("--scope", default="agn2/codex")
    record_parser.add_argument("--related-task", default="")
    record_parser.add_argument("--author", default="codex")
    record_parser.add_argument("--confidence", choices=["low", "medium", "high"], default="high")
    record_parser.add_argument("--constraint", action="append", default=[])
    record_parser.add_argument("--follow-up", action="append", default=[])
    record_parser.add_argument("--write-memory", action="store_true")
    record_parser.add_argument("--no-write", action="store_true")

    refresh_parser = sub.add_parser("refresh-report", help="Convert an agn-memory-refresh report into append-only operating memory records")
    refresh_parser.add_argument("--report-path", required=True)
    refresh_parser.add_argument("--scope", default="agn2/codex")
    refresh_parser.add_argument("--related-task", default="")
    refresh_parser.add_argument("--author", default="codex")
    refresh_parser.add_argument("--confidence", choices=["low", "medium", "high"], default="high")
    refresh_parser.add_argument("--write-memory", action="store_true")
    refresh_parser.add_argument("--no-write", action="store_true")

    args = parser.parse_args()

    if args.command == "record":
        records = build_manual_records(
            change_kind=str(args.change_kind).strip().lower(),
            name=str(args.name).strip(),
            summary=str(args.summary).strip(),
            operating_impact=str(args.operating_impact).strip(),
            source_refs=[str(item).strip() for item in list(args.source_ref or []) if str(item).strip()],
            scope=str(args.scope).strip(),
            related_task=str(args.related_task).strip(),
            author=str(args.author).strip(),
            confidence=str(args.confidence).strip().lower(),
            constraints=[str(item).strip() for item in list(args.constraint or []) if str(item).strip()],
            follow_ups=[str(item).strip() for item in list(args.follow_up or []) if str(item).strip()],
        )
        payload = {
            "ok": True,
            "generated_at": utc_now_iso(),
            "mode": "record",
            "records": records,
        }
        if args.write_memory:
            payload["dispatch_results"] = dispatch_records(records)
        if not args.no_write:
            payload["report_path"] = str(_write_report(payload, label=str(args.name)))
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0

    records = build_refresh_records(
        report_path=str(args.report_path).strip(),
        scope=str(args.scope).strip(),
        related_task=str(args.related_task).strip(),
        author=str(args.author).strip(),
        confidence=str(args.confidence).strip().lower(),
    )
    payload = {
        "ok": True,
        "generated_at": utc_now_iso(),
        "mode": "refresh-report",
        "records": records,
    }
    if args.write_memory:
        payload["dispatch_results"] = dispatch_records(records)
    if not args.no_write:
        payload["report_path"] = str(_write_report(payload, label="refresh-report"))
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
