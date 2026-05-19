from __future__ import annotations

from agn.architecture import evolution_pipeline as pipeline


def test_package_evolution_pipeline_exposes_package_metadata(monkeypatch) -> None:
    monkeypatch.setattr(
        pipeline,
        "build_external_toolbox_inventory",
        lambda: {"count": 0, "entries": []},
    )
    monkeypatch.setattr(
        pipeline,
        "build_infrastructure_map",
        lambda: {"modules": []},
    )
    monkeypatch.setattr(
        pipeline,
        "_registry",
        lambda: {"pipelines": [], "integration_tiers": [], "future_risks": []},
    )
    payload = pipeline.build_evolution_pipeline()
    assert payload["package_path"] == "agn.architecture.evolution_pipeline"
    assert payload["legacy_script_shim"] == "scripts/agn_evolution_pipeline.py"
