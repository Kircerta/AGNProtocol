from __future__ import annotations

from agn.governance import commands as acp


def test_package_commands_metadata() -> None:
    assert acp.PACKAGE_PATH == "agn.governance.commands"
    assert acp.LEGACY_SCRIPT_SHIM == "scripts/admin_command_protocol.py"


def test_package_commands_exports_submit_and_validate() -> None:
    assert callable(acp.submit_admin_command)
    assert callable(acp.validate_admin_command)
