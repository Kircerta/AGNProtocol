from __future__ import annotations

import json
from pathlib import Path

from scripts.agn_memory_recall import query_memory_recall
from scripts.agn_tool_reality_cards import resolve_tool_reality_cards


ROOT = Path(__file__).resolve().parents[1]
HOST_FIXTURE = ROOT / "testing" / "fixtures" / "federated_host_state" / "macbook_air.json"
EXAMPLE_CATALOG = ROOT / "config" / "tool_reality_cards.example.json"


def _card(payload: dict, tool_id: str) -> dict:
    for item in payload["cards"]:
        if item["tool_identity"]["tool_id"] == tool_id:
            return item
    raise AssertionError(f"card not found: {tool_id}")


def _write_test_catalog(tmp_path: Path, browser_use_bin: Path) -> Path:
    catalog = json.loads(EXAMPLE_CATALOG.read_text(encoding="utf-8"))
    for card in catalog["cards"]:
        if card["tool_id"] == "browser-use":
            card["path_checks"] = [str(browser_use_bin)]
        if card["tool_id"] == "agn_browser_use_wrapper":
            card["path_checks"] = [str(browser_use_bin)]
    catalog_path = tmp_path / "tool_reality_cards.test.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    return catalog_path


def test_reality_cards_cover_core_tools_and_host_specific_differences(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGN_HOST_STATE_PATH", str(HOST_FIXTURE))
    monkeypatch.setenv("HOME", str(tmp_path))
    browser_use_bin = tmp_path / ".browser-use-env" / "bin" / "browser-use"
    browser_use_bin.parent.mkdir(parents=True, exist_ok=True)
    browser_use_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("AGN_TOOL_REALITY_CARDS_PATH", str(_write_test_catalog(tmp_path, browser_use_bin)))

    payload = resolve_tool_reality_cards()
    assert payload["summary"]["count"] >= 5

    gui_agent = _card(payload, "gui_agent")
    assert gui_agent["current_host_availability"]["status"] == "unavailable"
    assert gui_agent["host_specific_notes"]

    qwen_local = _card(payload, "qwen_local")
    assert qwen_local["current_host_availability"]["status"] == "unavailable"

    browser_use = _card(payload, "browser-use")
    assert browser_use["current_host_availability"]["status"] in {"suitable", "limited"}
    assert browser_use["session_limitations"]
    boundary_text = f"{browser_use['authority_boundary']} {' '.join(browser_use['session_limitations'])}"
    assert "Profile or CDP" in boundary_text or "profile or CDP" in boundary_text


def test_memory_recall_surfaces_relevant_reality_cards(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGN_HOST_STATE_PATH", str(HOST_FIXTURE))
    monkeypatch.setenv("HOME", str(tmp_path))
    browser_use_bin = tmp_path / ".browser-use-env" / "bin" / "browser-use"
    browser_use_bin.parent.mkdir(parents=True, exist_ok=True)
    browser_use_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("AGN_TOOL_REALITY_CARDS_PATH", str(_write_test_catalog(tmp_path, browser_use_bin)))

    payload = query_memory_recall(
        task_summary="Use browser-use on my logged-in Chrome to monitor Twitter AI news.",
        task_type="social_monitoring",
        tools=["browser-use"],
    )
    ids = {item["tool_identity"]["tool_id"] for item in payload["tool_reality_cards"]}
    assert "browser-use" in ids
    browser_use = next(item for item in payload["tool_reality_cards"] if item["tool_identity"]["tool_id"] == "browser-use")
    assert browser_use["current_host_availability"]["status"] in {"suitable", "limited"}
