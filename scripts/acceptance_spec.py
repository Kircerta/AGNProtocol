#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "config" / "acceptance_spec_schema.json"

try:
    from pointer_protocol import read_ref_text, write_json_artifact
except ImportError:  # pragma: no cover - package import fallback
    from scripts.pointer_protocol import read_ref_text, write_json_artifact


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def load_schema() -> dict[str, Any]:
    if not SCHEMA_PATH.exists():
        return {}
    try:
        payload = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _norm_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def _default_items(*, acceptance_criteria: list[dict[str, Any]], review_requested: bool, require_metric: bool) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for idx, criterion in enumerate(acceptance_criteria, start=1):
        if not isinstance(criterion, dict):
            continue
        ac_id = str(criterion.get("id", "")).strip() or f"AC-{idx}"
        statement = str(criterion.get("text", "")).strip() or f"criterion {idx}"
        items.append(
            {
                "ac_id": ac_id,
                "statement": statement[:480],
                "evidence_type": "log_ref",
                "required": False,
                "evidence_refs": [],
            }
        )

    items.append(
        {
            "ac_id": "AC-EVIDENCE-EXEC",
            "statement": "execution evidence ref must exist before delivery",
            "evidence_type": "log_ref",
            "required": True,
            "evidence_refs": [],
        }
    )

    if review_requested:
        items.append(
            {
                "ac_id": "AC-EVIDENCE-REVIEW",
                "statement": "review verdict ref required when review is enabled",
                "evidence_type": "verdict_ref",
                "required": True,
                "evidence_refs": [],
            }
        )

    if require_metric:
        items.append(
            {
                "ac_id": "AC-EVIDENCE-PERF",
                "statement": "perf evidence ref required",
                "evidence_type": "metric",
                "required": True,
                "evidence_refs": [],
            }
        )
    return items


def build_acceptance_spec(
    *,
    task_id: str,
    trace_id: str,
    acceptance_criteria: list[dict[str, Any]],
    review_requested: bool,
    require_metric: bool = False,
    blocking: bool = True,
) -> dict[str, Any]:
    now = utc_now_iso()
    return {
        "task_id": str(task_id).strip(),
        "trace_id": str(trace_id).strip(),
        "blocking": bool(blocking),
        "created_at": now,
        "updated_at": now,
        "items": _default_items(
            acceptance_criteria=acceptance_criteria,
            review_requested=review_requested,
            require_metric=require_metric,
        ),
    }


def validate_acceptance_spec(spec: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not isinstance(spec, dict):
        return False, ["spec must be object"]

    for key in ("task_id", "trace_id"):
        value = spec.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{key} must be non-empty string")

    if not isinstance(spec.get("blocking"), bool):
        errors.append("blocking must be boolean")

    items = spec.get("items")
    if not isinstance(items, list) or not items:
        errors.append("items must be non-empty array")
        return len(errors) == 0, errors

    known_ids: set[str] = set()
    for idx, raw in enumerate(items):
        if not isinstance(raw, dict):
            errors.append(f"items[{idx}] must be object")
            continue
        ac_id = str(raw.get("ac_id", "")).strip()
        if not ac_id:
            errors.append(f"items[{idx}].ac_id required")
        elif ac_id in known_ids:
            errors.append(f"duplicate ac_id: {ac_id}")
        known_ids.add(ac_id)

        statement = str(raw.get("statement", "")).strip()
        if not statement:
            errors.append(f"items[{idx}].statement required")

        evidence_type = str(raw.get("evidence_type", "")).strip()
        if evidence_type not in {"log_ref", "result_ref", "verdict_ref", "patch_ref", "metric"}:
            errors.append(f"items[{idx}].evidence_type invalid: {evidence_type}")

        required = raw.get("required")
        if not isinstance(required, bool):
            errors.append(f"items[{idx}].required must be boolean")

        refs = raw.get("evidence_refs")
        if not isinstance(refs, list):
            errors.append(f"items[{idx}].evidence_refs must be list")
            continue
        for j, ref in enumerate(refs):
            if not isinstance(ref, str) or not ref.startswith("agn://"):
                errors.append(f"items[{idx}].evidence_refs[{j}] must be agn:// ref")

    return len(errors) == 0, errors


def load_acceptance_spec(ref: str) -> dict[str, Any]:
    text = read_ref_text(str(ref), mode="all", max_bytes=512 * 1024)
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("acceptance_spec_not_object")
    return payload


def ensure_acceptance_spec(
    *,
    store: Any,
    task: dict[str, Any],
    trace_id: str,
    attempt: int,
) -> tuple[dict[str, Any], str, bool, list[str]]:
    ref = str(task.get("acceptance_spec_ref", "")).strip()
    if ref.startswith("agn://"):
        try:
            spec = load_acceptance_spec(ref)
            ok, errs = validate_acceptance_spec(spec)
            if ok:
                return task, ref, False, []
            return task, ref, False, errs
        except Exception:
            # fallback to regeneration
            pass

    criteria = _norm_list(task.get("acceptance_criteria"))
    review_requested = bool(task.get("review_requested", False))
    require_metric = bool(task.get("require_metric_evidence", False))
    spec = build_acceptance_spec(
        task_id=str(task.get("id", "")).strip(),
        trace_id=str(trace_id).strip(),
        acceptance_criteria=[item for item in criteria if isinstance(item, dict)],
        review_requested=review_requested,
        require_metric=require_metric,
        blocking=True,
    )
    ok, errors = validate_acceptance_spec(spec)
    if not ok:
        return task, "", False, errors

    artifact = write_json_artifact(
        task_id=str(task.get("id", "")).strip(),
        attempt=max(1, int(attempt or 1)),
        artifact_id="acceptance_spec",
        payload=spec,
        filename="acceptance_spec.json",
        source="acceptance_spec",
    )
    task["acceptance_spec_ref"] = artifact.ref
    store.save_task(task)
    return task, artifact.ref, True, []


def apply_evidence_updates(spec: dict[str, Any], evidence_by_ac_id: dict[str, list[str]]) -> tuple[dict[str, Any], bool]:
    updated = json.loads(json.dumps(spec, ensure_ascii=True))
    changed = False
    now = utc_now_iso()
    items = updated.get("items", []) if isinstance(updated, dict) else []
    if not isinstance(items, list):
        return spec, False

    for item in items:
        if not isinstance(item, dict):
            continue
        ac_id = str(item.get("ac_id", "")).strip()
        refs = evidence_by_ac_id.get(ac_id)
        if refs is None:
            continue
        clean = [str(ref).strip() for ref in refs if isinstance(ref, str) and str(ref).strip().startswith("agn://")]
        if clean != item.get("evidence_refs"):
            item["evidence_refs"] = clean
            changed = True

    if changed:
        updated["updated_at"] = now
        return updated, True
    return spec, False
