from __future__ import annotations

from agn.architecture import infrastructure_map as infra


def test_package_infrastructure_map_exposes_package_metadata(monkeypatch) -> None:
    monkeypatch.setattr(
        infra,
        "build_capability_snapshot",
        lambda: {"surfaces": {}},
    )
    monkeypatch.setattr(
        infra,
        "_registry",
        lambda: {"districts": [], "modules": []},
    )
    payload = infra.build_infrastructure_map()
    assert payload["package_path"] == "agn.architecture.infrastructure_map"
    assert payload["legacy_script_shim"] == "scripts/agn_infrastructure_map.py"
