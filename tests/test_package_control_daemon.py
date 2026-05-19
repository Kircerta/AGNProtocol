from __future__ import annotations

from agn.governance import control_daemon as cd


def test_package_control_daemon_exposes_metadata() -> None:
    assert cd.PACKAGE_PATH == "agn.governance.control_daemon"
    assert cd.LEGACY_SCRIPT_SHIM == "scripts/control_daemon.py"


def test_package_control_daemon_run_once_is_callable() -> None:
    assert callable(cd.run_once)
    assert callable(cd.run_loop)
