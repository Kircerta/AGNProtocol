from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import kirara_sense


def test_web_search_requires_api_key_env(monkeypatch) -> None:
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    rc = kirara_sense._run_web_search(
        query="openclaw",
        count=3,
        key_env="BRAVE_API_KEY",
        timeout_sec=1.0,
    )
    assert rc == 1


def test_parse_tsv_lines_handles_empty_and_rows() -> None:
    assert kirara_sense._parse_tsv_lines("", ["a", "b"]) == []
    parsed = kirara_sense._parse_tsv_lines("x\ty\nz\tw", ["c1", "c2"])
    assert parsed == [{"c1": "x", "c2": "y"}, {"c1": "z", "c2": "w"}]
