from __future__ import annotations

from scripts.agent_runner import _compose_codex_prompt


def test_compose_codex_prompt_is_task_driven() -> None:
    request = "Fix stale bookmark refresh race in PathHealthWatchdog."
    criteria = [{"id": "AC-1", "text": "produce reproducible verification evidence"}]
    prompt = _compose_codex_prompt(request_text=request, acceptance_criteria=criteria)

    assert request in prompt
    assert "launch database error" not in prompt.lower()
    assert "Acceptance criteria:" in prompt
