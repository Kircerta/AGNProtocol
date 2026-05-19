from __future__ import annotations

from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import provider_registry as pr
from provider_registry import load_registry, probe_capabilities, resolve_executor_provider, resolve_reviewer_provider


def test_provider_resolve_uses_registry_defaults() -> None:
    reg = load_registry()
    assert resolve_executor_provider("", reg) == "codex"
    assert resolve_reviewer_provider("", reg) == "gemini"
    assert resolve_executor_provider("gemini", reg) == "gemini"
    assert resolve_executor_provider("claude", reg) == "claude"
    assert resolve_executor_provider("qwen_local", reg) == "qwen_local"
    assert resolve_reviewer_provider("codex", reg) == "codex"
    assert resolve_reviewer_provider("unknown", reg) == "gemini"
    assert resolve_reviewer_provider("deepseek", reg) == "deepseek"
    assert resolve_reviewer_provider("qwen_local", reg) == "qwen_local"


def test_probe_capabilities_reflects_cli_and_api_env(monkeypatch: object) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")
    with tempfile.NamedTemporaryFile() as tmp_model:
        monkeypatch.setenv("QWEN_LOCAL_MODEL", tmp_model.name)
        monkeypatch.setenv("QWEN_LOCAL_BASE_URL", "http://127.0.0.1:8765/v1")
        monkeypatch.setenv("VERTEX_LOCAL_BASE_URL", "http://127.0.0.1:8099/v1")
        monkeypatch.setattr(pr, "_check_local_api_endpoint", lambda base_url: (True, ""))

        caps = probe_capabilities(load_registry())
        assert caps["default_executor"] == "codex"
        assert caps["default_reviewer"] == "gemini"
        assert "codex" in caps["executors"]
        assert "gemini" in caps["executors"]
        assert "claude" in caps["executors"]
        assert "qwen_local" in caps["executors"]
        assert "vertex_local" in caps["executors"]
        assert "codex" in caps["reviewers"]
        assert "gemini" in caps["reviewers"]
        assert "claude" in caps["reviewers"]
        assert "deepseek" in caps["reviewers"]
        assert "qwen_local" in caps["reviewers"]
        assert "vertex_local" in caps["reviewers"]
        assert caps["reviewers"]["deepseek"]["kind"] == "api"
        assert caps["reviewers"]["deepseek"]["has_api_key"] is True
        assert caps["reviewers"]["qwen_local"]["kind"] == "api"
        assert caps["reviewers"]["qwen_local"]["requires_api_key"] is False
        assert caps["reviewers"]["qwen_local"]["available"] is True
        assert caps["reviewers"]["vertex_local"]["available"] is True


def test_probe_capabilities_marks_local_vertex_unreachable(monkeypatch: object) -> None:
    monkeypatch.setenv("VERTEX_LOCAL_BASE_URL", "http://127.0.0.1:8099/v1")
    monkeypatch.setenv("VERTEX_LOCAL_MODEL", "gemini-2.5-flash")
    monkeypatch.setattr(pr, "_check_local_api_endpoint", lambda base_url: (False, "local_provider_endpoint_unreachable:127.0.0.1:8099"))

    caps = probe_capabilities(load_registry())
    assert caps["reviewers"]["vertex_local"]["available"] is False
    assert caps["reviewers"]["vertex_local"]["unavailable_reason"] == "local_provider_endpoint_unreachable:127.0.0.1:8099"


def test_probe_capabilities_marks_local_qwen_unreachable(monkeypatch: object) -> None:
    with tempfile.NamedTemporaryFile() as tmp_model:
        monkeypatch.setenv("QWEN_LOCAL_MODEL", tmp_model.name)
        monkeypatch.setenv("QWEN_LOCAL_BASE_URL", "http://127.0.0.1:8765/v1")
        monkeypatch.setattr(pr, "_check_local_api_endpoint", lambda base_url: (False, "local_provider_endpoint_unreachable:127.0.0.1:8765"))

        caps = probe_capabilities(load_registry())

    assert caps["executors"]["qwen_local"]["available"] is False
    assert caps["executors"]["qwen_local"]["unavailable_reason"] == "local_provider_endpoint_unreachable:127.0.0.1:8765"
    assert caps["reviewers"]["qwen_local"]["available"] is False
    assert caps["reviewers"]["qwen_local"]["unavailable_reason"] == "local_provider_endpoint_unreachable:127.0.0.1:8765"
