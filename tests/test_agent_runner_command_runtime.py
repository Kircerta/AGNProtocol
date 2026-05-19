from __future__ import annotations

from pathlib import Path

from scripts.agent_runner import run_command


ROOT = Path(__file__).resolve().parents[1]


def test_run_command_executable_not_found_returns_127(tmp_path: Path) -> None:
    outcome = run_command(
        cmd=["__agn_missing_binary_for_test__"],
        cwd=ROOT,
        timeout_sec=2.0,
        log_path=tmp_path / "missing-bin.log",
    )
    assert outcome.return_code == 127
    assert "EXECUTABLE_NOT_FOUND" in outcome.stderr
