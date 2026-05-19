from __future__ import annotations

from agn.governance import execution_gateway as gateway
from agn.governance import handler_cli_guard as guard


def test_package_execution_gateway_describes_real_surface() -> None:
    payload = gateway.describe_gateway()
    assert payload["phase_alignment"] == "phase_3_gradual_implementation_migration"
    assert payload["origin_phase"] == "phase_2_governance_enforcement_boundary"
    assert payload["package_path"] == "agn.governance.execution_gateway"
    assert payload["legacy_script_shim"] == "scripts/agn_governed_execution.py"
    assert "provider" in payload["cli_commands"]


def test_package_handler_cli_guard_blocks_without_ack() -> None:
    assert guard.should_block_direct_handler_cli(False) is True
    payload = guard.build_direct_handler_cli_block(
        handler_id="model_router",
        purpose="provider handler",
        recommended_entrypoints=["python3 scripts/agn_governed_execution.py provider --from-json-file <task.json>"],
    )
    assert payload["handler_id"] == "model_router"
    assert payload["error"] == "direct_handler_cli_requires_explicit_ack"
