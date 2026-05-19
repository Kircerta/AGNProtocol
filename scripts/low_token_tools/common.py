from __future__ import annotations

import argparse
import copy
import json
import os
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any, Callable

import httpx

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from provider_registry import load_registry


ProviderSettings = dict[str, Any]
ValidationFn = Callable[[dict[str, Any]], list[str]]
PromptBuilder = Callable[[dict[str, Any]], str]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    sample_list_fields: tuple[str, ...]
    prompt_builder: PromptBuilder
    validate_input: ValidationFn
    validate_output: ValidationFn


def _extract_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None

    candidates: list[str] = [raw]
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        if isinstance(parsed.get("result"), str):
            candidates.append(str(parsed.get("result")))
        if isinstance(parsed.get("response"), str):
            candidates.append(str(parsed.get("response")))
        return parsed

    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        candidates.append(raw[start : end + 1])

    for candidate in candidates:
        try:
            loaded = json.loads(candidate)
        except Exception:
            continue
        if isinstance(loaded, dict):
            if isinstance(loaded.get("result"), str):
                nested = _extract_json_object(str(loaded.get("result")))
                if nested is not None:
                    return nested
            if isinstance(loaded.get("response"), str):
                nested = _extract_json_object(str(loaded.get("response")))
                if nested is not None:
                    return nested
            return loaded
    return None


def _extract_openai_message_text(decoded: dict[str, Any]) -> str:
    choices = decoded.get("choices", [])
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0]
    if not isinstance(choice, dict):
        return ""
    message = choice.get("message", {})
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = str(item.get("text", "")).strip()
                    if text:
                        parts.append(text)
            return "\n".join(parts).strip()
    text = choice.get("text")
    return str(text).strip() if isinstance(text, str) else ""


def _api_settings(provider: str) -> ProviderSettings:
    registry = load_registry()
    spec = registry.get("executors", {}).get(provider)
    if not isinstance(spec, dict):
        spec = registry.get("reviewers", {}).get(provider)
    if not isinstance(spec, dict):
        raise ValueError(f"unknown_provider:{provider}")
    if str(spec.get("kind", "")).strip().lower() != "api":
        raise ValueError(f"provider_not_api:{provider}")

    api_key_env = str(spec.get("api_key_env", "")).strip()
    base_url_env = str(spec.get("base_url_env", "")).strip()
    model_env = str(spec.get("model_env", "")).strip()
    api_key = str(os.getenv(api_key_env, "")).strip() if api_key_env else ""
    base_url = str(os.getenv(base_url_env, "")).strip() if base_url_env else ""
    model = str(os.getenv(model_env, "")).strip() if model_env else ""
    if not base_url:
        base_url = str(spec.get("default_base_url", "")).strip()
    if not model:
        model = str(spec.get("default_model", "")).strip()
    requires_api_key = bool(spec.get("requires_api_key", bool(api_key_env)))
    return {
        "provider": provider,
        "api_key": api_key,
        "api_key_env": api_key_env,
        "base_url": base_url,
        "model": model,
        "requires_api_key": requires_api_key,
    }


def _call_provider(*, provider: str, prompt: str, max_tokens: int, timeout_sec: float) -> tuple[dict[str, Any] | None, str]:
    settings = _api_settings(provider)
    if not settings["base_url"] or not settings["model"]:
        return None, "provider_missing_base_url_or_model"
    if settings["requires_api_key"] and not settings["api_key"]:
        missing = str(settings["api_key_env"] or "api_key").strip()
        return None, f"provider_api_key_missing:{missing}"

    endpoint = str(settings["base_url"]).rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if settings["api_key"]:
        headers["Authorization"] = f"Bearer {settings['api_key']}"

    payload = {
        "model": str(settings["model"]),
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a bounded data transformation worker. "
                    "Return exactly one JSON object and nothing else."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "max_tokens": max_tokens,
    }
    try:
        with httpx.Client(timeout=timeout_sec) as client:
            response = client.post(endpoint, headers=headers, json=payload)
    except httpx.TimeoutException:
        return None, "provider_timeout"
    except Exception as exc:
        return None, f"provider_exception:{type(exc).__name__}:{str(exc)[:300]}"

    if response.status_code >= 400:
        return None, f"provider_http_{response.status_code}:{response.text[:600]}"

    try:
        decoded = response.json()
    except Exception:
        return None, f"provider_invalid_json:{response.text[:600]}"

    content = _extract_openai_message_text(decoded)
    if not content:
        return None, "provider_empty_content"

    parsed = _extract_json_object(content)
    if parsed is None:
        return None, f"provider_non_json_content:{content[:600]}"
    return parsed, ""


def _apply_sample(payload: dict[str, Any], sample_list_fields: tuple[str, ...], sample_size: int) -> tuple[dict[str, Any], bool]:
    if sample_size <= 0:
        return copy.deepcopy(payload), False
    sampled = copy.deepcopy(payload)
    applied = False
    for field in sample_list_fields:
        value = sampled.get(field)
        if isinstance(value, list) and len(value) > sample_size:
            sampled[field] = value[:sample_size]
            applied = True
    return sampled, applied


def _envelope(
    *,
    ok: bool,
    tool: str,
    provider: str,
    sample_size: int,
    sample_applied: bool,
    input_path: str,
    output_path: str,
    result: dict[str, Any] | None = None,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "tool": tool,
        "provider": provider,
        "sample_size": sample_size,
        "sample_applied": sample_applied,
        "input_path": input_path,
        "output_path": output_path,
        "errors": errors or [],
        "result": result or {},
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def run_tool(
    *,
    spec: ToolSpec,
    input_path: Path,
    output_path: Path,
    provider: str,
    sample_size: int,
    max_tokens: int,
    timeout_sec: float,
) -> tuple[int, dict[str, Any]]:
    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
    except Exception as exc:
        envelope = _envelope(
            ok=False,
            tool=spec.name,
            provider=provider,
            sample_size=sample_size,
            sample_applied=False,
            input_path=str(input_path),
            output_path=str(output_path),
            errors=[f"input_load_failed:{type(exc).__name__}:{str(exc)[:200]}"],
        )
        _write_json(output_path, envelope)
        return 1, envelope

    if not isinstance(payload, dict):
        envelope = _envelope(
            ok=False,
            tool=spec.name,
            provider=provider,
            sample_size=sample_size,
            sample_applied=False,
            input_path=str(input_path),
            output_path=str(output_path),
            errors=["input_validation_failed:payload_must_be_json_object"],
        )
        _write_json(output_path, envelope)
        return 1, envelope

    sampled_payload, sample_applied = _apply_sample(payload, spec.sample_list_fields, sample_size)
    input_errors = spec.validate_input(sampled_payload)
    if input_errors:
        envelope = _envelope(
            ok=False,
            tool=spec.name,
            provider=provider,
            sample_size=sample_size,
            sample_applied=sample_applied,
            input_path=str(input_path),
            output_path=str(output_path),
            errors=[f"input_validation_failed:{err}" for err in input_errors],
        )
        _write_json(output_path, envelope)
        return 1, envelope

    prompt = spec.prompt_builder(sampled_payload)
    raw_result, error = _call_provider(
        provider=provider,
        prompt=prompt,
        max_tokens=max_tokens,
        timeout_sec=timeout_sec,
    )
    if raw_result is None:
        envelope = _envelope(
            ok=False,
            tool=spec.name,
            provider=provider,
            sample_size=sample_size,
            sample_applied=sample_applied,
            input_path=str(input_path),
            output_path=str(output_path),
            errors=[error or "provider_failed"],
        )
        _write_json(output_path, envelope)
        return 1, envelope

    output_errors = spec.validate_output(raw_result)
    if output_errors:
        envelope = _envelope(
            ok=False,
            tool=spec.name,
            provider=provider,
            sample_size=sample_size,
            sample_applied=sample_applied,
            input_path=str(input_path),
            output_path=str(output_path),
            errors=[f"output_validation_failed:{err}" for err in output_errors],
            result=raw_result,
        )
        _write_json(output_path, envelope)
        return 1, envelope

    envelope = _envelope(
        ok=True,
        tool=spec.name,
        provider=provider,
        sample_size=sample_size,
        sample_applied=sample_applied,
        input_path=str(input_path),
        output_path=str(output_path),
        result=raw_result,
    )
    _write_json(output_path, envelope)
    return 0, envelope


def build_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--input", required=True, help="Input JSON payload")
    parser.add_argument("--output", required=True, help="Output JSON file")
    parser.add_argument("--provider", default="qwen_local", choices=["qwen_local", "deepseek"])
    parser.add_argument("--sample-size", type=int, default=0, help="Truncate list inputs for a sample-check run")
    parser.add_argument("--max-tokens", type=int, default=900)
    parser.add_argument("--timeout-sec", type=float, default=180.0)
    return parser
