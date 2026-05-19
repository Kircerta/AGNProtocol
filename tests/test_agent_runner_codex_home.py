from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import agent_runner


def test_prepare_agn_codex_home_bootstraps_clean_home(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    source = home / ".codex"
    source.mkdir(parents=True)
    (source / "auth.json").write_text('{"token":"x"}\n', encoding="utf-8")
    (source / "config.toml").write_text('model = "gpt-5.4"\n', encoding="utf-8")
    (source / "AGENTS.md").write_text("codex notes\n", encoding="utf-8")

    target = home / ".codex_agn_custom"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGN_CODEX_HOME", str(target))

    prepared = agent_runner._prepare_agn_codex_home()

    assert prepared == target
    assert (target / "auth.json").read_text(encoding="utf-8") == '{"token":"x"}\n'
    assert (target / "config.toml").read_text(encoding="utf-8") == 'model = "gpt-5.4"\n'
    assert (target / "AGENTS.md").read_text(encoding="utf-8") == "codex notes\n"
    assert (target / "shell_snapshots").is_dir()
    assert (target / "sessions").is_dir()
    assert (target / "tmp").is_dir()


def test_codex_runner_env_uses_prepared_home(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    source = home / ".codex"
    source.mkdir(parents=True)
    (source / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGN_CODEX_HOME", str(home / ".codex_agn"))

    env = agent_runner._codex_runner_env()

    assert env["CODEX_HOME"] == str(home / ".codex_agn")
    assert Path(env["CODEX_HOME"]).is_dir()
