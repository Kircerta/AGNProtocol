from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.agn_federated_read_model import build_federated_read_model
from scripts.agn_host_posture_brief import build_host_posture_brief
from scripts.agn_host_state_heartbeat import build_host_state_heartbeat_model


ROOT = Path(__file__).resolve().parents[1]


def _run_json(script: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(ROOT / script)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_federated_read_model_is_paused() -> None:
    payload = build_federated_read_model()
    assert payload["status"] == "paused"
    assert payload["reason"] == "multi_host_read_model_paused"


def test_host_state_heartbeat_is_paused() -> None:
    payload = build_host_state_heartbeat_model()
    assert payload["status"] == "paused"
    assert payload["reason"] == "multi_host_heartbeat_paused"


def test_host_posture_brief_is_paused() -> None:
    payload = build_host_posture_brief(task_summary="Use Chrome on this machine.")
    assert payload["status"] == "paused"
    assert payload["reason"] == "multi_host_posture_paused"


def test_runtime_router_cli_is_paused() -> None:
    payload = _run_json("scripts/agn_runtime_router.py")
    assert payload["status"] == "paused"
    assert payload["reason"] == "multi_host_runtime_router_paused"


def test_federated_runtime_acceptance_cli_is_paused() -> None:
    payload = _run_json("scripts/validation/run_federated_runtime_acceptance.py")
    assert payload["status"] == "paused"
    assert payload["reason"] == "multi_host_runtime_validation_paused"
