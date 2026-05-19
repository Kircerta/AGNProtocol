from __future__ import annotations

from agn.governance import review_contract as rc


def test_package_review_contract_exposes_metadata() -> None:
    assert rc.PACKAGE_PATH == "agn.governance.review_contract"
    assert rc.LEGACY_SCRIPT_SHIM == "scripts/review_contract.py"


def test_extract_json_object_finds_embedded_payload() -> None:
    payload = rc.extract_json_object("prefix {\"verdict\":\"approve\"} suffix")
    assert payload == {"verdict": "approve"}


def test_merge_structured_verdicts_preserves_highest_severity() -> None:
    merged = rc.merge_structured_verdicts(
        [
            {"verdict": "approve", "confidence": "high", "core_reasoning": ["a"]},
            {"verdict": "reject", "confidence": "medium", "risks": ["missing proof"]},
        ]
    )
    assert merged["verdict"] == "reject"
    assert "missing proof" in merged["risks"]
