from __future__ import annotations

from scripts.model_router import build_route_decision, classify_task, run_routed_task


def test_classify_json_task_defaults_to_structured_profile() -> None:
    decision = classify_task(
        {
            "prompt": "Extract invoice id and amount.",
            "response_mode": "json_object",
            "risk_level": "low",
            "logical_complexity": "low",
            "verification_cost": "low",
        }
    )
    assert decision["task"]["task_profile"] == "structured_transform"
    assert "structured_output_requested" in decision["reasons"]


def test_route_prefers_qwen_for_low_risk_bounded_work(monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.model_router.probe_capabilities",
        lambda *_args, **_kwargs: {
            "executors": {
                "qwen_local": {"available": True},
                "gemini": {"available": True},
                "claude": {"available": True},
            },
            "reviewers": {"deepseek": {"available": False}},
        },
    )
    decision = build_route_decision(
        {
            "prompt": "Normalize these labels.",
            "task_type": "label_normalization",
            "response_mode": "json_object",
            "risk_level": "low",
            "logical_complexity": "low",
            "verification_cost": "low",
            "cost_sensitivity": "high",
        }
    )
    assert decision["selected_provider"] == "qwen_local"
    assert any(item["provider"] == "deepseek" for item in decision["filtered_providers"])


def test_route_prefers_claude_for_high_complexity_high_risk(monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.model_router.probe_capabilities",
        lambda *_args, **_kwargs: {
            "executors": {"qwen_local": {"available": True}, "gemini": {"available": True}, "claude": {"available": True}},
            "reviewers": {"deepseek": {"available": True}},
        },
    )
    decision = build_route_decision(
        {
            "prompt": "Review a subtle distributed systems design.",
            "task_type": "complex_reasoning",
            "risk_level": "high",
            "logical_complexity": "very_high",
            "verification_cost": "high",
            "cost_sensitivity": "medium",
        }
    )
    assert decision["selected_provider"] == "claude"
    assert any(item["provider"] == "qwen_local" and item["reason"].startswith("profile_not_allowed") for item in decision["filtered_providers"])


def test_gemini_lane_defaults_to_flash_for_light_work_and_pro_for_hard_work(monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.model_router.probe_capabilities",
        lambda *_args, **_kwargs: {
            "executors": {"qwen_local": {"available": False}, "gemini": {"available": True}, "claude": {"available": True}},
            "reviewers": {"deepseek": {"available": False}},
        },
    )
    light = build_route_decision(
        {
            "prompt": "Extract fields.",
            "task_type": "json_extraction",
            "response_mode": "json_object",
            "risk_level": "low",
            "logical_complexity": "low",
            "verification_cost": "low",
            "cost_sensitivity": "high",
        }
    )
    hard = build_route_decision(
        {
            "prompt": "Review a subtle concurrency design.",
            "task_type": "review",
            "response_mode": "text",
            "risk_level": "high",
            "logical_complexity": "high",
            "verification_cost": "high",
            "cost_sensitivity": "medium",
        }
    )
    light_gemini = next(item for item in light["candidate_chain"] if item["provider"] == "gemini")
    hard_gemini = next(item for item in hard["candidate_chain"] if item["provider"] == "gemini")
    assert light_gemini["model_name"] == "flash"
    assert hard_gemini["model_name"] == "pro"


def test_run_routed_task_falls_back_when_primary_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "scripts.model_router.probe_capabilities",
        lambda *_args, **_kwargs: {
            "executors": {"qwen_local": {"available": True}, "gemini": {"available": True}, "claude": {"available": True}},
            "reviewers": {"deepseek": {"available": False}},
        },
    )

    def fake_invoke_provider(*, provider, task, timeout_sec, log_path=None, registry=None, model_override=""):
        if provider == "qwen_local":
            return False, {"error": "provider_timeout", "duration_ms": 5.0}
        if provider == "gemini":
            return True, {"duration_ms": 8.0, "content": "{\"status\":\"ok\"}", "parsed": {"status": "ok"}, "model_override": model_override}
        return False, {"error": "unexpected_provider"}

    monkeypatch.setattr("scripts.model_router.invoke_provider", fake_invoke_provider)
    envelope = run_routed_task(
        {
            "task_id": "router-fallback-test",
            "prompt": "Return status ok.",
            "task_type": "structured_transform",
            "response_mode": "json_object",
            "risk_level": "low",
            "logical_complexity": "low",
            "verification_cost": "low",
            "cost_sensitivity": "high",
            "allow_fallback": True,
        },
        output_path=tmp_path / "router-fallback-test.json",
    )
    assert envelope["ok"] is True
    assert envelope["route_decision"]["selected_provider"] == "gemini"
    assert envelope["route_decision"]["fallback_from"] == "qwen_local"
    assert [item["provider"] for item in envelope["attempts"]] == ["qwen_local", "qwen_local", "gemini"]
    assert envelope["attempts"][-1]["model_name"] == "flash"
