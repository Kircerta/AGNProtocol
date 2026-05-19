from __future__ import annotations

import json
from pathlib import Path

from scripts import agn_external_toolbox as toolbox


def test_build_toolbox_entry_reports_repo_and_binary_state(monkeypatch, tmp_path: Path) -> None:
    open_source_root = tmp_path / "OpenSource"
    repo = open_source_root / "browser-use"
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("# browser-use\n", encoding="utf-8")
    monkeypatch.setattr(toolbox.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "browser-use" else "")
    entry = {
        "repo_dir": "browser-use",
        "docs_relpath": "README.md",
        "category": "browser_automation",
        "summary": "x",
        "binary_checks": ["browser-use"],
        "safe_capabilities": ["browser automation"],
        "agn_fit": ["execution-layer browser adapter"],
        "boundaries": ["execution only"],
        "preferred_surfaces": ["scripts/desktop_adapter.py"],
    }

    payload = toolbox.build_toolbox_entry("browser-use", entry, open_source_root=open_source_root)
    assert payload["repo_exists"] is True
    assert payload["docs_exists"] is True
    assert payload["readiness"] == "available"
    assert payload["binary_checks"][0]["available"] is True


def test_build_inventory_reads_catalog(monkeypatch, tmp_path: Path) -> None:
    catalog_path = tmp_path / "external_toolbox.json"
    catalog_path.write_text(
        json.dumps(
            {
                "version": "1.0.0",
                "entries": {
                    "superpowers": {
                        "repo_dir": "superpowers",
                        "docs_relpath": "README.md",
                        "category": "workflow",
                        "summary": "workflow"
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    repo = tmp_path / "OpenSource" / "superpowers"
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("# superpowers\n", encoding="utf-8")
    monkeypatch.setattr(toolbox, "CATALOG_PATH", catalog_path)
    monkeypatch.setattr(toolbox, "DEFAULT_OPEN_SOURCE_ROOT", tmp_path / "OpenSource")

    payload = toolbox.build_inventory()
    assert payload["count"] == 1
    assert payload["entries"][0]["name"] == "superpowers"
    assert payload["categories"]["workflow"] == 1


def test_catalog_declares_beads_with_runtime_binary_checks() -> None:
    catalog = toolbox.load_catalog()
    entry = catalog["entries"]["beads"]
    assert entry["category"] == "task_graph"
    assert entry["mount_mode"] == "runtime_optional"
    assert entry["binary_checks"] == ["bd", "dolt"]
    assert any("canonical memory" in item for item in entry["boundaries"])
