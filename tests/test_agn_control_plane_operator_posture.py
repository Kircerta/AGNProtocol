from __future__ import annotations

from scripts.agn_control_plane_operator_posture import build_payload


def test_control_plane_posture_prefers_formal_command_for_gate_actions() -> None:
    payload = build_payload(
        task_summary="Approve gate after operator review and keep the action visible in Control Plane",
        risk_level="medium",
        explicit_flags={
            "needs_human_visibility": False,
            "needs_governed_write": False,
            "needs_queue_observation": False,
            "needs_system_truth": False,
            "needs_history": False,
        },
        command="APPROVE_GATE",
        target_id="gate-123",
        reason="Admin approved the gate",
    )
    assert payload["primary_surface"] == "formal_command_path"
    assert payload["formal_command_path"]["required"] is True
    assert payload["formal_command_path"]["envelope_preview"]["command"] == "APPROVE_GATE"


def test_control_plane_posture_prefers_read_model_for_observation() -> None:
    payload = build_payload(
        task_summary="Inspect dispatcher queue and canonical status before acting",
        risk_level="low",
        explicit_flags={
            "needs_human_visibility": False,
            "needs_governed_write": False,
            "needs_queue_observation": False,
            "needs_system_truth": False,
            "needs_history": False,
        },
        command="",
        target_id="",
        reason="",
    )
    assert payload["primary_surface"] == "read_model"
    assert any(item["surface"] == "read_model" for item in payload["surface_sequence"])


def test_control_plane_posture_maps_pause_gate_to_hold_gate() -> None:
    payload = build_payload(
        task_summary="Pause gate until the operator returns",
        risk_level="medium",
        explicit_flags={
            "needs_human_visibility": False,
            "needs_governed_write": False,
            "needs_queue_observation": False,
            "needs_system_truth": False,
            "needs_history": False,
        },
        command="",
        target_id="gate-42",
        reason="Admin requested hold",
    )
    assert payload["formal_command_path"]["command"] == "HOLD_GATE"
    assert "formal admin command is actually submitted" in payload["rollback_and_abort_semantics"][0]
