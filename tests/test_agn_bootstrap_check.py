from __future__ import annotations

from pathlib import Path

from scripts import agn_bootstrap_check as abc


def test_build_bootstrap_check_marks_required_ready_with_stubbed_dependencies(monkeypatch, tmp_path: Path) -> None:
    fake_root = tmp_path / "repo"
    fake_root.mkdir()
    for rel in ("pyproject.toml", "AGENTS.md", "PROJECT_BRIEF.md", "RUNBOOK.md", "scripts/agn2_system.py", "scripts/agn2_execution_workflow.py", "src/agn"):
        path = fake_root / rel
        if rel.endswith("agn"):
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")

    monkeypatch.setattr(abc, "ROOT", fake_root)

    payload = abc.build_bootstrap_check(
        which=lambda name: f"/usr/local/bin/{name}",
        find_spec=lambda name: object(),
    )

    assert payload["status"]["required_ready"] is True
    assert payload["status"]["first_task_ready"] is True
    assert payload["provider_lanes"]["available_count"] >= 1


def test_build_bootstrap_check_fails_when_required_dependencies_missing(monkeypatch, tmp_path: Path) -> None:
    fake_root = tmp_path / "repo"
    fake_root.mkdir()
    monkeypatch.setattr(abc, "ROOT", fake_root)

    payload = abc.build_bootstrap_check(
        which=lambda _name: None,
        find_spec=lambda _name: None,
    )

    assert payload["status"]["required_ready"] is False
    assert payload["status"]["first_task_ready"] is False
    assert any("Install the missing required commands" in step for step in payload["next_steps"])


def test_build_bootstrap_check_allows_missing_optional_python_modules(monkeypatch, tmp_path: Path) -> None:
    fake_root = tmp_path / "repo"
    fake_root.mkdir()
    for rel in ("pyproject.toml", "AGENTS.md", "PROJECT_BRIEF.md", "RUNBOOK.md", "scripts/agn2_system.py", "scripts/agn2_execution_workflow.py"):
        path = fake_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    monkeypatch.setattr(abc, "ROOT", fake_root)

    payload = abc.build_bootstrap_check(
        which=lambda name: f"/usr/local/bin/{name}" if name in {"git", "python3", "uv"} else None,
        find_spec=lambda _name: None,
    )

    assert payload["status"]["required_ready"] is True
    assert payload["status"]["first_task_ready"] is True
