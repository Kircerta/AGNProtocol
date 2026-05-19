from __future__ import annotations

import json
from pathlib import Path

from scripts.agn_host_state_probe import collect_host_state, output_paths_for_host, run_self_check, write_host_state
from scripts.federated_host_state import validate_host_state_payload


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "testing" / "fixtures" / "federated_host_state"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def test_output_paths_are_stable_and_include_local_and_host_specific_names() -> None:
    paths = output_paths_for_host("Example Laptop portable")
    assert len(paths) == 1
    assert paths[0].name == "federated_host_state.local.json"


def test_self_check_reports_missing_required_tool() -> None:
    payload = _load_fixture("macbook_air.json")
    report = run_self_check(payload, require_tools=["gui_agent"], require_wrappers=[], require_providers=[])
    assert report["ok"] is False
    assert report["failures"] == [
        {
            "kind": "tool",
            "name": "gui_agent",
            "details": "not_synced_to_this_host",
        }
    ]


def test_local_collect_payload_validates() -> None:
    payload = collect_host_state(stale_after_sec=120)
    result = validate_host_state_payload(payload)
    assert result.valid, result.errors
    assert payload["heartbeat"]["stale_after_sec"] == 120
    assert payload["host_identity"]["host_id"]


def test_write_host_state_prunes_host_specific_alias_files(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGN_REPO_ROOT", str(tmp_path))
    read_models = tmp_path / "runtime" / "admin_control" / "read_models"
    read_models.mkdir(parents=True, exist_ok=True)
    payload = _load_fixture("macbook_air.json")
    payload["host_identity"]["host_id"] = "macbook-air-current"
    stale_alias = _load_fixture("macbook_air.json")
    stale_alias["host_identity"]["host_id"] = "macbook-air-old"
    (read_models / "federated_host_state.macbook-air-old.json").write_text(json.dumps(stale_alias), encoding="utf-8")

    write_host_state(payload)

    assert not (read_models / "federated_host_state.macbook-air-old.json").exists()
    assert (read_models / "federated_host_state.local.json").exists()
