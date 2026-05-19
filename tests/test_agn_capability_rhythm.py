from __future__ import annotations

from scripts.agn_capability_rhythm import build_rhythm_payload, infer_needs


def test_infer_needs_detects_desktop_and_worker_language() -> None:
    payload = infer_needs("Use screenshots to inspect the GUI and delegate bounded OCR cleanup")
    assert payload["needs_desktop"] is True
    assert payload["needs_worker"] is True


def test_build_rhythm_payload_recommends_visual_and_delegate_skills() -> None:
    payload = build_rhythm_payload(
        task_summary="Inspect a screenshot, plan GUI actions, and delegate OCR cleanup",
        risk_level="low",
        explicit_flags={},
    )
    selected = {item["name"] for item in payload["selected_skills"]}
    assert "agn-system-entry" in selected
    assert "agn-visual-operator" in selected
    assert "agn-bounded-delegation" in selected
    assert payload["provider_plan"]["controller"]["provider"] == "codex"


def test_build_rhythm_payload_recommends_coding_overlay() -> None:
    payload = build_rhythm_payload(
        task_summary="Implement a new feature safely and add tests",
        risk_level="medium",
        explicit_flags={},
    )
    overlays = {item["name"] for item in payload["recommended_overlays"]}
    assert "coding-criticality" in overlays
