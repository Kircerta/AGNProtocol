from __future__ import annotations

import json
from pathlib import Path

from scripts.federated_host_state import load_schema, validate_host_state_payload


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "testing" / "fixtures" / "federated_host_state"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def test_schema_exists_and_has_expected_root_shape() -> None:
    schema = load_schema()
    assert schema["title"] == "AGN Federated Host State"
    assert schema["properties"]["schema_version"]["const"] == "agn.host_state.v1"
    assert schema["required"] == [
        "schema_version",
        "host_identity",
        "static_facts",
        "runtime_facts",
        "heartbeat",
    ]


def test_macstudio_sample_validates() -> None:
    payload = _load_fixture("macstudio.json")
    result = validate_host_state_payload(payload)
    assert result.valid, result.errors


def test_ubuntu_sample_validates() -> None:
    payload = _load_fixture("ubuntu.json")
    result = validate_host_state_payload(payload)
    assert result.valid, result.errors


def test_macbook_air_sample_validates() -> None:
    payload = _load_fixture("macbook_air.json")
    result = validate_host_state_payload(payload)
    assert result.valid, result.errors


def test_samples_cover_distinct_host_classes() -> None:
    host_classes = {
        _load_fixture("macstudio.json")["host_identity"]["host_class"],
        _load_fixture("ubuntu.json")["host_identity"]["host_class"],
        _load_fixture("macbook_air.json")["host_identity"]["host_class"],
    }
    assert host_classes == {"mac_desktop", "linux_server", "mac_laptop"}


def test_validator_rejects_runtime_fact_missing_heartbeat() -> None:
    payload = _load_fixture("macbook_air.json")
    payload.pop("heartbeat")
    result = validate_host_state_payload(payload)
    assert result.valid is False
    assert any("heartbeat must be object" in err for err in result.errors)
