from __future__ import annotations

from pathlib import Path

from scripts import capability_snapshot as cs


def test_build_capability_snapshot_includes_surfaces_and_skills(monkeypatch, tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex_agn"
    skills_root = codex_home / "skills"
    (skills_root / "agn-system-entry").mkdir(parents=True)
    (skills_root / "agn-system-entry" / "SKILL.md").write_text("---\nname: agn-system-entry\ndescription: x\n---\n", encoding="utf-8")
    (skills_root / "gh-fix-ci").mkdir(parents=True)
    (skills_root / "gh-fix-ci" / "SKILL.md").write_text("---\nname: gh-fix-ci\ndescription: x\n---\n", encoding="utf-8")
    (skills_root / ".system" / "skill-creator").mkdir(parents=True)
    (skills_root / ".system" / "skill-creator" / "SKILL.md").write_text("---\nname: skill-creator\ndescription: x\n---\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(cs, "probe_capabilities", lambda: {"default_executor": "codex", "default_reviewer": "gemini", "executors": {"codex": {"available": True}}, "reviewers": {"gemini": {"available": True}, "claude": {"available": False}}})
    monkeypatch.setattr(cs.shutil, "which", lambda name: f"/usr/bin/{name}" if name in {"python3", "sips", "tesseract", "ghostty", "cargo"} else "")
    monkeypatch.setattr(cs, "GUI_AGENT_BIN", tmp_path / "gui-agent")
    cs.GUI_AGENT_BIN.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(cs, "CONTROL_PLANE_APP", tmp_path / "AGN2.0 Control Plane.app")
    cs.CONTROL_PLANE_APP.mkdir()
    monkeypatch.setattr(cs, "CONTROL_PLANE_INSTALLED_APP", tmp_path / "Applications" / "AGN2.0 Control Plane.app")
    cs.CONTROL_PLANE_INSTALLED_APP.parent.mkdir(parents=True, exist_ok=True)
    cs.CONTROL_PLANE_INSTALLED_APP.mkdir()
    monkeypatch.setattr(cs, "CONVERSATION_MONITOR_APP", tmp_path / "AGN Conversation Monitor.app")
    cs.CONVERSATION_MONITOR_APP.mkdir()
    monkeypatch.setattr(cs, "CONVERSATION_MONITOR_INSTALLED_APP", tmp_path / "Applications" / "AGN Conversation Monitor.app")

    payload = cs.build_capability_snapshot()
    assert payload["surfaces"]["dispatcher"]["available"] is True
    assert payload["surfaces"]["desktop_control"]["available"] is True
    assert payload["surfaces"]["external_toolbox"]["category"] == "execution_support"
    assert payload["surfaces"]["cognitive_overlays"]["category"] == "execution_support"
    assert payload["surfaces"]["host_info"]["category"] == "runtime"
    assert payload["surfaces"]["infrastructure_map"]["category"] == "execution_support"
    assert payload["surfaces"]["evolution_pipeline"]["category"] == "execution_support"
    assert payload["surfaces"]["reconstruction_status"]["category"] == "execution_support"
    assert payload["surfaces"]["governed_execution_gateway"]["category"] == "execution_support"
    assert payload["surfaces"]["task_start_kernel"]["category"] == "execution_support"
    assert payload["surfaces"]["operator_brief"]["category"] == "execution_support"
    assert payload["modules"]["vision_parser"]["produces"][-1] == "optional *.evidence.* artifacts when redaction is triggered"
    assert "agn-system-entry" in payload["skills"]["agn_specific"]
    assert "skill-creator" in payload["skills"]["system_skills"]
    assert payload["toolbox"]["count"] >= 1
    assert payload["cognitive_overlays"]
    assert payload["surfaces"]["control_plane"]["category"] == "authority_control"
    assert "host_info" in payload["surface_taxonomy"]["runtime"]
    assert payload["surface_taxonomy"]["review"] == ["flagship_review"]
    assert "infrastructure_map" in payload["surface_taxonomy"]["execution_support"]
    assert "evolution_pipeline" in payload["surface_taxonomy"]["execution_support"]
    assert "reconstruction_status" in payload["surface_taxonomy"]["execution_support"]
    assert "governed_execution_gateway" in payload["surface_taxonomy"]["execution_support"]
    assert "task_start_kernel" in payload["surface_taxonomy"]["execution_support"]
    assert "operator_brief" in payload["surface_taxonomy"]["execution_support"]
    assert payload["provider_policy"]["reviewer_policy"]["preferred_order"] == ["claude", "gemini"]
    assert payload["provider_policy"]["provider_roles"]["deepseek"]["forbidden_for"][0] == "final_review"
    assert payload["toolchain"]["control_plane_app"]["installed_app_path"].endswith("AGN2.0 Control Plane.app")
    assert payload["modules"]["memory_recorder"]["retention_policy"]["delete_in_place"] is False
    assert payload["modules"]["memory_recorder"]["retention_policy"]["invalid_append_policy"]
    assert "security_boundary" in payload["modules"]["desktop_adapter"]
    assert "quarantine_sensitive_auth_or_secret_surfaces_before_gui_execution" in payload["modules"]["vision_parser"]["security_boundary"]
    assert "emit_redacted_ocr_outputs_plus_security_scan_artifacts_when_sensitive_hits_are_detected" in payload["modules"]["vision_parser"]["security_boundary"]
    assert "preserve_raw_visual_evidence_in_additive_artifacts_for_audited_human_review_not_default_consumption" in payload["modules"]["vision_parser"]["security_boundary"]
    assert payload["modules"]["reviewer"]["abort_policy"]


def test_vision_surface_requires_tesseract_and_sips(monkeypatch) -> None:
    monkeypatch.setattr(cs, "probe_capabilities", lambda: {"default_executor": "codex", "default_reviewer": "gemini", "executors": {}, "reviewers": {}})
    monkeypatch.setattr(cs.shutil, "which", lambda name: "/usr/bin/sips" if name == "sips" else "")
    payload = cs.build_capability_snapshot()
    assert payload["surfaces"]["vision_parser"]["available"] is False
