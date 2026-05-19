from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import sync_repo_skills  # type: ignore


FORBIDDEN_FRAGMENTS = (
    str(Path.home()),
    "<repo-root>",
)

PORTABLE_SURFACES = [
    ROOT / "AGENTS.md",
    ROOT / "RUNBOOK.md",
    ROOT / "documentation" / "admin" / "CODEX_PERSONALIZATION_AGENT.md",
    ROOT / "documentation" / "reference" / "agn2_codex_operating_memory.md",
]

TEXT_SUFFIXES = {
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".sh",
    ".swift",
    ".txt",
    ".yaml",
    ".yml",
}


def test_repo_skill_surfaces_do_not_embed_machine_specific_repo_paths() -> None:
    paths = PORTABLE_SURFACES + sorted((ROOT / "skills").rglob("*"))
    checked = [
        path
        for path in paths
        if path.is_file() and (path.suffix in TEXT_SUFFIXES or path.name == "SKILL.md")
    ]
    assert checked
    for path in checked:
        text = path.read_text(encoding="utf-8")
        for fragment in FORBIDDEN_FRAGMENTS:
            assert fragment not in text, f"{fragment} leaked into {path}"


def test_redirect_note_uses_portable_repo_root_commands_and_labels() -> None:
    note = sync_repo_skills._redirect_note(
        "shared",
        "example-skill",
        ROOT / "skills" / "shared" / "example-skill",
        Path.home() / ".codex" / "skills" / "example-skill",
    )
    assert "python3 scripts/sync_repo_skills.py install --group shared --skill example-skill" in note
    assert "canonical source: `skills/shared/example-skill`" in note
    assert "installed copy: `$CODEX_HOME/skills/example-skill`" in note
    for fragment in FORBIDDEN_FRAGMENTS:
        assert fragment not in note
