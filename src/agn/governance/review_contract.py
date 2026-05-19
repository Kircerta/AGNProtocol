"""Structured review-verdict contract helpers.

This is the real package implementation for AGN's structured review contract.
The legacy script remains only as a compatibility shim.
"""
from __future__ import annotations

import json
from typing import Any


PACKAGE_PATH = "agn.governance.review_contract"
LEGACY_SCRIPT_SHIM = "scripts/review_contract.py"

VERDICT_ORDER = {
    "approve": 0,
    "revise": 1,
    "reject": 2,
    "escalate": 3,
}
CONFIDENCE_VALUES = {"low", "medium", "high"}


def structured_verdict_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "verdict",
            "confidence",
            "core_reasoning",
            "risks",
            "missing_evidence",
            "recommended_action",
            "escalate_to_human",
        ],
        "additionalProperties": True,
        "properties": {
            "verdict": {"type": "string", "enum": list(VERDICT_ORDER)},
            "confidence": {"type": "string", "enum": sorted(CONFIDENCE_VALUES)},
            "core_reasoning": {"type": "array", "items": {"type": "string"}},
            "risks": {"type": "array", "items": {"type": "string"}},
            "missing_evidence": {"type": "array", "items": {"type": "string"}},
            "recommended_action": {"type": "array", "items": {"type": "string"}},
            "escalate_to_human": {"type": "boolean"},
        },
    }


def extract_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    candidates = [raw]
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        candidates.append(raw[start : end + 1])
    for candidate in candidates:
        try:
            decoded = json.loads(candidate)
        except Exception:
            continue
        if isinstance(decoded, dict):
            return decoded
    return None


def _normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def normalize_structured_verdict(raw: dict[str, Any] | None) -> dict[str, Any]:
    payload = raw if isinstance(raw, dict) else {}
    verdict = str(payload.get("verdict", "")).strip().lower()
    if verdict not in VERDICT_ORDER:
        verdict = "reject"
    confidence = str(payload.get("confidence", "")).strip().lower()
    if confidence not in CONFIDENCE_VALUES:
        confidence = "medium"
    normalized = {
        "verdict": verdict,
        "confidence": confidence,
        "core_reasoning": _normalize_string_list(payload.get("core_reasoning")),
        "risks": _normalize_string_list(payload.get("risks")),
        "missing_evidence": _normalize_string_list(payload.get("missing_evidence")),
        "recommended_action": _normalize_string_list(payload.get("recommended_action")),
        "escalate_to_human": bool(payload.get("escalate_to_human", False)) or verdict == "escalate",
    }
    if not normalized["core_reasoning"]:
        normalized["core_reasoning"] = ["No structured reasoning provided."]
    return normalized


def merge_structured_verdicts(verdicts: list[dict[str, Any]]) -> dict[str, Any]:
    if not verdicts:
        return normalize_structured_verdict({"verdict": "escalate", "confidence": "low", "escalate_to_human": True})

    normalized = [normalize_structured_verdict(item) for item in verdicts]
    dominant = max(normalized, key=lambda item: VERDICT_ORDER[item["verdict"]])

    def _merge(key: str) -> list[str]:
        seen: set[str] = set()
        merged: list[str] = []
        for item in normalized:
            for entry in item.get(key, []):
                text = str(entry).strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                merged.append(text)
        return merged

    return {
        "verdict": dominant["verdict"],
        "confidence": dominant["confidence"],
        "core_reasoning": _merge("core_reasoning") or dominant["core_reasoning"],
        "risks": _merge("risks"),
        "missing_evidence": _merge("missing_evidence"),
        "recommended_action": _merge("recommended_action"),
        "escalate_to_human": any(bool(item.get("escalate_to_human")) for item in normalized),
    }


def legacy_decision_from_structured(verdict: dict[str, Any]) -> str:
    normalized = normalize_structured_verdict(verdict)
    return "approve" if normalized["verdict"] == "approve" else "reject"


__all__ = [
    "CONFIDENCE_VALUES",
    "LEGACY_SCRIPT_SHIM",
    "PACKAGE_PATH",
    "VERDICT_ORDER",
    "extract_json_object",
    "legacy_decision_from_structured",
    "merge_structured_verdicts",
    "normalize_structured_verdict",
    "structured_verdict_schema",
]
