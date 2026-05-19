#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from provider_registry import load_registry, probe_capabilities
from agn_handler_cli_guard import render_direct_handler_cli_block, should_block_direct_handler_cli


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


CONFIG_PATH = ROOT / "config" / "model_router.json"
REPORTS_DIR = ROOT / "reports" / "model_router"
RISK_ORDER = {"low": 0, "medium": 1, "high": 2}
COMPLEXITY_ORDER = {"low": 0, "medium": 1, "high": 2, "very_high": 3}
COST_ORDER = {"low": 0, "medium": 1, "high": 2}
SUPPORTED_PROFILES = {
    "structured_transform",
    "json_extraction",
    "label_normalization",
    "ocr_cleanup",
    "batch_cleaning",
    "bounded_summarization",
    "general_analysis",
    "complex_reasoning",
    "review",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "default_provider_order": ["qwen_local", "deepseek", "gemini", "claude"],
    "default_timeout_sec": 120.0,
    "default_retry_count": 0,
    "profile_aliases": {
        "text_normalization": "structured_transform",
        "json_extraction": "json_extraction",
        "label_normalization": "label_normalization",
        "ocr_cleanup": "ocr_cleanup",
        "batch_record_cleaning": "batch_cleaning",
        "bounded_summarization": "bounded_summarization",
        "general_analysis": "general_analysis",
        "complex_reasoning": "complex_reasoning",
        "review": "review",
    },
    "profile_bias": {},
    "provider_policies": {},
}


def _resolve_provider_lane(provider: str, task: dict[str, Any], policy: dict[str, Any]) -> dict[str, str]:
    lane = ""
    model_name = ""
    effective_cost_tier = str(policy.get("cost_tier", "")).strip()
    if provider == "gemini":
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        hint = str(metadata.get("gemini_model_hint", "")).strip().lower() if isinstance(metadata, dict) else ""
        lane_map = policy.get("model_lane_by_profile", {})
        if hint:
            lane = hint
        elif isinstance(lane_map, dict):
            lane = str(lane_map.get(task["task_profile"], "")).strip().lower()
        name_map = policy.get("model_name_by_lane", {})
        if isinstance(name_map, dict):
            model_name = str(name_map.get(lane, "")).strip()
        if not model_name:
            model_name = lane or "flash"
        cost_map = policy.get("cost_tier_by_lane", {})
        if isinstance(cost_map, dict):
            effective_cost_tier = str(cost_map.get(lane, effective_cost_tier)).strip()
    return {
        "lane": lane,
        "model_name": model_name,
        "effective_cost_tier": effective_cost_tier,
    }


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def load_router_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return json.loads(json.dumps(DEFAULT_CONFIG))
    if not isinstance(payload, dict):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    merged.update(payload)
    for key in ("profile_aliases", "profile_bias", "provider_policies"):
        base = DEFAULT_CONFIG.get(key, {})
        custom = payload.get(key, {})
        if isinstance(base, dict) and isinstance(custom, dict):
            merged[key] = {**base, **custom}
    return merged


def _normalize_level(value: Any, *, allowed: dict[str, int], default: str) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in allowed else default


def _extract_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    candidates: list[str] = [raw]
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        candidates.append(raw[start : end + 1])
    for candidate in candidates:
        try:
            decoded = json.loads(candidate)
        except Exception:
            continue
        if isinstance(decoded, dict):
            return decoded
    return None


def _parse_cli_json_output(text: str) -> tuple[dict[str, Any] | None, str]:
    parsed = _extract_json_object(text)
    if parsed is None:
        return None, "provider_non_json_content"
    return parsed, ""


def _append_log(log_path: Path, title: str, text: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    body = f"===== {title} =====\n{text}"
    if not body.endswith("\n"):
        body += "\n"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(body)


def _run_cli_command(*, cmd: list[str], timeout_sec: float, log_path: Path) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=timeout_sec,
            check=False,
        )
        duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        _append_log(
            log_path,
            "command",
            "\n".join(
                [
                    f"timestamp={utc_now_iso()}",
                    f"command={' '.join(cmd)}",
                    f"return_code={completed.returncode}",
                    f"duration_ms={duration_ms}",
                    "--- STDOUT ---",
                    completed.stdout or "",
                    "--- STDERR ---",
                    completed.stderr or "",
                ]
            ),
        )
        return {
            "return_code": int(completed.returncode),
            "stdout": str(completed.stdout or ""),
            "stderr": str(completed.stderr or ""),
            "duration_ms": duration_ms,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        _append_log(
            log_path,
            "command",
            "\n".join(
                [
                    f"timestamp={utc_now_iso()}",
                    f"command={' '.join(cmd)}",
                    "return_code=124",
                    f"duration_ms={duration_ms}",
                    "timed_out=True",
                    "--- STDOUT ---",
                    str(exc.stdout or ""),
                    "--- STDERR ---",
                    str(exc.stderr or ""),
                ]
            ),
        )
        return {
            "return_code": 124,
            "stdout": str(exc.stdout or ""),
            "stderr": str(exc.stderr or ""),
            "duration_ms": duration_ms,
            "timed_out": True,
        }
    except FileNotFoundError as exc:
        duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        _append_log(log_path, "command", f"timestamp={utc_now_iso()}\ncommand={' '.join(cmd)}\nerror={exc}")
        return {
            "return_code": 127,
            "stdout": "",
            "stderr": f"EXECUTABLE_NOT_FOUND:{exc}",
            "duration_ms": duration_ms,
            "timed_out": False,
        }


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


def _extract_usage(decoded: dict[str, Any]) -> dict[str, int]:
    """Extract token usage from an OpenAI-compatible API response."""
    usage = decoded.get("usage")
    if not isinstance(usage, dict):
        return {}
    result: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                "input_tokens", "output_tokens"):
        val = usage.get(key)
        if isinstance(val, (int, float)):
            result[key] = int(val)
    # Normalize: ensure input/output keys exist.
    if "input_tokens" not in result and "prompt_tokens" in result:
        result["input_tokens"] = result["prompt_tokens"]
    if "output_tokens" not in result and "completion_tokens" in result:
        result["output_tokens"] = result["completion_tokens"]
    if "total_tokens" not in result:
        result["total_tokens"] = result.get("input_tokens", 0) + result.get("output_tokens", 0)
    return result


def _append_usage_ledger(provider: str, model_name: str, task_id: str, usage: dict[str, int]) -> None:
    """Append a usage entry to the provider usage ledger (JSONL)."""
    if not usage:
        return
    ledger_path = REPORTS_DIR.parent / "provider_usage.jsonl"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": utc_now_iso(),
        "provider": provider,
        "model_name": model_name,
        "task_id": task_id,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }
    try:
        with ledger_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=True) + "\n")
    except Exception:
        pass


def _api_settings(provider: str, registry: dict[str, Any]) -> dict[str, Any]:
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
    return {
        "provider": provider,
        "api_key_env": api_key_env,
        "api_key": str(os.getenv(api_key_env, "")).strip() if api_key_env else "",
        "base_url": str(os.getenv(base_url_env, "")).strip() if base_url_env else "",
        "model": str(os.getenv(model_env, "")).strip() if model_env else "",
        "requires_api_key": bool(spec.get("requires_api_key", bool(api_key_env))),
        "default_base_url": str(spec.get("default_base_url", "")).strip(),
        "default_model": str(spec.get("default_model", "")).strip(),
    }


def _build_prompt(task: dict[str, Any]) -> str:
    response_mode = str(task.get("response_mode", "text")).strip()
    json_schema = task.get("json_schema")
    rules = [
        f"Task profile: {task['task_profile']}",
        f"Risk level: {task['risk_level']}",
        f"Logical complexity: {task['logical_complexity']}",
        f"Verification cost: {task['verification_cost']}",
        "You are a subordinate model worker, not the controller.",
    ]
    if response_mode == "json_object":
        rules.append("Return exactly one JSON object and nothing else.")
    else:
        rules.append("Return only the requested answer with no extra preamble.")
    if isinstance(json_schema, dict) and json_schema:
        rules.append("Follow this JSON schema guidance exactly:")
        rules.append(json.dumps(json_schema, ensure_ascii=True, indent=2))
    prompt_parts = ["\n".join(rules), "", str(task.get("prompt", "")).strip()]
    return "\n".join(prompt_parts).strip()


def normalize_task(raw: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    conf = config or load_router_config()
    if not isinstance(raw, dict):
        raise ValueError("task_payload_must_be_object")
    prompt = str(raw.get("prompt", "")).strip()
    if not prompt:
        raise ValueError("prompt_is_required")
    raw_type = str(raw.get("task_type", "")).strip().lower()
    aliases = conf.get("profile_aliases", {})
    task_type = str(aliases.get(raw_type, raw_type) or "").strip().lower()
    response_mode = "json_object" if str(raw.get("response_mode", "")).strip().lower() == "json_object" else "text"
    batch_size = _safe_int(raw.get("batch_size", 1), 1)
    risk_level = _normalize_level(raw.get("risk_level"), allowed=RISK_ORDER, default="low")
    logical_complexity = _normalize_level(raw.get("logical_complexity"), allowed=COMPLEXITY_ORDER, default="low")
    verification_cost = _normalize_level(raw.get("verification_cost"), allowed=COST_ORDER, default="low")
    cost_sensitivity = _normalize_level(raw.get("cost_sensitivity"), allowed=COST_ORDER, default="high")

    if task_type not in SUPPORTED_PROFILES:
        if response_mode == "json_object" and batch_size > 1:
            task_type = "batch_cleaning"
        elif response_mode == "json_object":
            task_type = "structured_transform"
        elif logical_complexity in {"high", "very_high"}:
            task_type = "complex_reasoning"
        else:
            task_type = "general_analysis"

    return {
        "task_id": str(raw.get("task_id") or f"router-{int(time.time())}").strip(),
        "task_profile": task_type,
        "prompt": prompt,
        "response_mode": response_mode,
        "json_schema": raw.get("json_schema") if isinstance(raw.get("json_schema"), dict) else {},
        "risk_level": risk_level,
        "logical_complexity": logical_complexity,
        "verification_cost": verification_cost,
        "cost_sensitivity": cost_sensitivity,
        "batch_size": max(1, batch_size),
        "preferred_provider": str(raw.get("preferred_provider", "")).strip().lower(),
        "allow_fallback": bool(raw.get("allow_fallback", False)),
        "metadata": raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
        "max_output_tokens": _safe_int(raw.get("max_output_tokens", 600), 600),
    }


def classify_task(task: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    conf = config or load_router_config()
    normalized = normalize_task(task, conf)
    reasons = [f"profile={normalized['task_profile']}"]
    if normalized["response_mode"] == "json_object":
        reasons.append("structured_output_requested")
    if normalized["batch_size"] > 1:
        reasons.append(f"batch_size={normalized['batch_size']}")
    reasons.append(f"risk={normalized['risk_level']}")
    reasons.append(f"logical_complexity={normalized['logical_complexity']}")
    reasons.append(f"verification_cost={normalized['verification_cost']}")
    reasons.append(f"cost_sensitivity={normalized['cost_sensitivity']}")
    return {
        "task": normalized,
        "reasons": reasons,
    }


def _level_allows(current: str, maximum: str, order: dict[str, int]) -> bool:
    return order[current] <= order[maximum]


def _provider_availability(provider: str, capabilities: dict[str, Any]) -> tuple[bool, str]:
    for bucket in ("executors", "reviewers"):
        section = capabilities.get(bucket, {})
        if isinstance(section, dict) and provider in section and isinstance(section[provider], dict):
            available = bool(section[provider].get("available", False))
            unavailable_reason = str(section[provider].get("unavailable_reason", "")).strip()
            reason = "available" if available else (unavailable_reason or "capability_probe_unavailable")
            return available, reason
    return False, "provider_not_registered"


def build_route_decision(task: dict[str, Any], *, config: dict[str, Any] | None = None, capabilities: dict[str, Any] | None = None) -> dict[str, Any]:
    conf = config or load_router_config()
    classified = classify_task(task, conf)
    normalized = classified["task"]
    caps = capabilities or probe_capabilities(load_registry())
    order = list(conf.get("default_provider_order", DEFAULT_CONFIG["default_provider_order"]))
    preferred = normalized.get("preferred_provider", "")
    if preferred and preferred in order:
        order = [preferred] + [provider for provider in order if provider != preferred]

    profile_bias = conf.get("profile_bias", {}).get(normalized["task_profile"], {})
    policies = conf.get("provider_policies", {})
    candidates: list[dict[str, Any]] = []
    filtered: list[dict[str, Any]] = []

    for index, provider in enumerate(order):
        policy = policies.get(provider)
        if not isinstance(policy, dict):
            filtered.append({"provider": provider, "reason": "missing_policy"})
            continue
        lane_info = _resolve_provider_lane(provider, normalized, policy)
        available, availability_reason = _provider_availability(provider, caps)
        if not available:
            filtered.append({"provider": provider, "reason": availability_reason})
            continue
        allowed_profiles = policy.get("allowed_profiles", [])
        if isinstance(allowed_profiles, list) and normalized["task_profile"] not in allowed_profiles:
            filtered.append({"provider": provider, "reason": f"profile_not_allowed:{normalized['task_profile']}"})
            continue
        max_risk = _normalize_level(policy.get("max_risk"), allowed=RISK_ORDER, default="high")
        if not _level_allows(normalized["risk_level"], max_risk, RISK_ORDER):
            filtered.append({"provider": provider, "reason": f"risk_exceeds_policy:{max_risk}"})
            continue
        max_complexity = _normalize_level(policy.get("max_complexity"), allowed=COMPLEXITY_ORDER, default="very_high")
        if not _level_allows(normalized["logical_complexity"], max_complexity, COMPLEXITY_ORDER):
            filtered.append({"provider": provider, "reason": f"complexity_exceeds_policy:{max_complexity}"})
            continue
        max_verification_cost = _normalize_level(policy.get("max_verification_cost"), allowed=COST_ORDER, default="high")
        if not _level_allows(normalized["verification_cost"], max_verification_cost, COST_ORDER):
            filtered.append({"provider": provider, "reason": f"verification_cost_exceeds_policy:{max_verification_cost}"})
            continue

        score = (len(order) - index) * 10 + int(profile_bias.get(provider, 0) or 0)
        reasons = [f"base_order_index={index}"]
        if lane_info["lane"]:
            reasons.append(f"model_lane={lane_info['lane']}")
        if lane_info["model_name"]:
            reasons.append(f"model_name={lane_info['model_name']}")
        if provider == preferred:
            score += 100
            reasons.append("preferred_provider_boost")
        if normalized["cost_sensitivity"] == "high":
            cost_tier = lane_info["effective_cost_tier"] or str(policy.get("cost_tier", "")).strip()
            if cost_tier == "metered_high":
                score -= 80
                reasons.append("high_cost_penalty")
            elif cost_tier == "metered_mid":
                score -= 25
                reasons.append("medium_cost_penalty")
            elif cost_tier == "metered_low":
                score -= 10
                reasons.append("light_cost_penalty")
            elif cost_tier == "free_local":
                score += 20
                reasons.append("free_local_bonus")
        if normalized["logical_complexity"] in {"high", "very_high"} and provider == "claude":
            score += 45
            reasons.append("complex_reasoning_bonus")
        if normalized["logical_complexity"] in {"high", "very_high"} and provider == "qwen_local":
            score -= 40
            reasons.append("complexity_penalty")
        if normalized["risk_level"] == "high" and provider == "claude":
            score += 25
            reasons.append("high_risk_bonus")
        candidates.append(
            {
                "provider": provider,
                "model_lane": lane_info["lane"],
                "model_name": lane_info["model_name"],
                "score": score,
                "timeout_sec": float(policy.get("timeout_sec", conf.get("default_timeout_sec", 120.0)) or 120.0),
                "retry_count": int(policy.get("retry_count", conf.get("default_retry_count", 0)) or 0),
                "reasons": reasons,
            }
        )

    candidates.sort(key=lambda item: (-int(item["score"]), order.index(str(item["provider"]))))
    operational_notes: list[str] = []
    qwen_filtered = next((item for item in filtered if str(item.get("provider", "")) == "qwen_local"), None)
    if isinstance(qwen_filtered, dict):
        qwen_reason = str(qwen_filtered.get("reason", "")).strip()
        if (
            qwen_reason.startswith("qwen_storage_mount_missing:")
            or qwen_reason.startswith("qwen_model_path_missing:")
            or qwen_reason.startswith("qwen_model_path_not_readable:")
            or qwen_reason.startswith("local_provider_endpoint_unreachable:")
        ):
            fallback_target = str(candidates[0]["provider"]) if candidates else ""
            operational_notes.append(f"qwen_local_on_hold:{qwen_reason}")
            if fallback_target and fallback_target != "qwen_local":
                operational_notes.append(f"qwen_local_fallback_provider:{fallback_target}")
    return {
        "task": normalized,
        "classification": {
            "task_profile": normalized["task_profile"],
            "reasons": classified["reasons"],
        },
        "candidate_chain": candidates,
        "filtered_providers": filtered,
        "operational_notes": operational_notes,
        "selected_provider": str(candidates[0]["provider"]) if candidates else "",
        "generated_at": utc_now_iso(),
    }


def _invoke_api_provider(*, provider: str, task: dict[str, Any], timeout_sec: float, registry: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    settings = _api_settings(provider, registry)
    base_url = settings["base_url"] or settings["default_base_url"]
    model = settings["model"] or settings["default_model"]
    api_key = settings["api_key"]
    if not base_url or not model:
        return False, {"error": "provider_missing_base_url_or_model"}
    if settings["requires_api_key"] and not api_key:
        return False, {"error": f"provider_api_key_missing:{settings['api_key_env'] or 'api_key'}"}

    prompt = _build_prompt(task)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "temperature": 0,
        "stream": False,
        "max_tokens": int(task.get("max_output_tokens", 600) or 600),
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a subordinate model worker. Keep output bounded, deterministic, and faithful. "
                    "Do not claim authority over destructive actions."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    }
    request_bytes = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    request = urllib.request.Request(
        str(base_url).rstrip("/") + "/chat/completions",
        data=request_bytes,
        headers=headers,
        method="POST",
    )

    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            response_code = int(getattr(response, "status", response.getcode()))
            response_text = response.read().decode("utf-8", errors="replace")
    except (TimeoutError, OSError) as exc:
        err_type = type(exc).__name__
        return False, {"error": f"provider_timeout:{err_type}", "duration_ms": round((time.perf_counter() - started) * 1000.0, 2)}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return False, {"error": f"provider_http_{exc.code}:{body[:500]}", "duration_ms": round((time.perf_counter() - started) * 1000.0, 2)}
    except urllib.error.URLError as exc:
        return False, {
            "error": f"provider_exception:URLError:{str(exc.reason)[:300]}",
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 2),
        }
    except Exception as exc:
        return False, {
            "error": f"provider_exception:{type(exc).__name__}:{str(exc)[:300]}",
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 2),
        }

    duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
    if response_code >= 400:
        return False, {"error": f"provider_http_{response_code}:{response_text[:500]}", "duration_ms": duration_ms}
    try:
        decoded = json.loads(response_text)
    except Exception:
        return False, {"error": f"provider_invalid_json:{response_text[:500]}", "duration_ms": duration_ms}

    content = _extract_openai_message_text(decoded)
    if not content:
        return False, {"error": "provider_empty_content", "duration_ms": duration_ms}
    # Extract token usage from OpenAI-compatible response.
    usage = _extract_usage(decoded)
    base_payload: dict[str, Any] = {"duration_ms": duration_ms, "content": content}
    if usage:
        base_payload["usage"] = usage
    if task["response_mode"] == "json_object":
        parsed = _extract_json_object(content)
        if parsed is None:
            return False, {"error": f"provider_non_json_content:{content[:500]}", "duration_ms": duration_ms}
        base_payload["parsed"] = parsed
    return True, base_payload


def _invoke_cli_provider(
    *,
    provider: str,
    task: dict[str, Any],
    timeout_sec: float,
    log_path: Path,
    model_override: str = "",
) -> tuple[bool, dict[str, Any]]:
    prompt = _build_prompt(task)
    if provider == "gemini":
        cmd = ["gemini", "-p", prompt]
        if str(model_override).strip():
            cmd.extend(["--model", str(model_override).strip()])
    elif provider == "claude":
        if task["response_mode"] == "json_object" and task["json_schema"]:
            cmd = [
                "claude",
                "-p",
                "--output-format",
                "json",
                "--json-schema",
                json.dumps(task["json_schema"], ensure_ascii=True),
                prompt,
            ]
        else:
            cmd = ["claude", "-p", prompt]
    else:
        return False, {"error": "unsupported_cli_provider"}

    outcome = _run_cli_command(cmd=cmd, timeout_sec=timeout_sec, log_path=log_path)
    if int(outcome["return_code"]) != 0 or bool(outcome["timed_out"]):
        reason = "provider_timeout" if bool(outcome["timed_out"]) else f"provider_nonzero_exit:{int(outcome['return_code'])}"
        return False, {
            "error": reason,
            "duration_ms": round(float(outcome["duration_ms"]), 2),
            "stderr": str(outcome["stderr"] or outcome["stdout"])[:500],
        }

    content = str(outcome["stdout"] or "").strip()
    if task["response_mode"] == "json_object":
        parsed, error = _parse_cli_json_output(content)
        if parsed is None:
            return False, {"error": error or "provider_non_json_content", "duration_ms": round(float(outcome["duration_ms"]), 2)}
        return True, {"duration_ms": round(float(outcome["duration_ms"]), 2), "content": content, "parsed": parsed}
    return True, {"duration_ms": round(float(outcome["duration_ms"]), 2), "content": content}


def invoke_provider(
    *,
    provider: str,
    task: dict[str, Any],
    timeout_sec: float,
    log_path: Path | None = None,
    registry: dict[str, Any] | None = None,
    model_override: str = "",
) -> tuple[bool, dict[str, Any]]:
    reg = registry or load_registry()
    spec = reg.get("executors", {}).get(provider)
    if not isinstance(spec, dict):
        spec = reg.get("reviewers", {}).get(provider)
    if not isinstance(spec, dict):
        return False, {"error": "provider_not_registered"}
    kind = str(spec.get("kind", "")).strip().lower()
    if kind == "api":
        return _invoke_api_provider(provider=provider, task=task, timeout_sec=timeout_sec, registry=reg)
    attempt_log = log_path or REPORTS_DIR / f"{task['task_id']}.{provider}.exec.log"
    attempt_log.parent.mkdir(parents=True, exist_ok=True)
    return _invoke_cli_provider(
        provider=provider,
        task=task,
        timeout_sec=timeout_sec,
        log_path=attempt_log,
        model_override=model_override,
    )


def _emit_fallback_alert(envelope: dict[str, Any]) -> None:
    """Alert admin via Telegram when provider fallback occurs or all providers fail."""
    try:
        from agn_notify_runtime import enqueue_message
    except ImportError:
        return
    route = envelope.get("route_decision", {})
    fallback_from = route.get("fallback_from", "")
    selected = route.get("selected_provider", "")
    task_id = envelope.get("task", {}).get("task_id", "unknown")
    ok = envelope.get("ok", False)
    attempts = envelope.get("attempts", [])
    errors = [a.get("error", "") for a in attempts if not a.get("ok") and a.get("error")]
    admin_chat_id = os.getenv("AGN_TELEGRAM_ADMIN_CHAT_ID", "").strip()
    if not admin_chat_id or not admin_chat_id.lstrip("-").isdigit():
        return
    if ok and fallback_from:
        text = (
            f"[AGN] provider fallback\n"
            f"primary={fallback_from} failed\n"
            f"routed_to={selected}\n"
            f"task_id={task_id}\n"
            f"errors={'; '.join(errors[:3])}"
        )
    elif not ok:
        text = (
            f"[AGN] all providers failed\n"
            f"task_id={task_id}\n"
            f"attempted={len(attempts)}\n"
            f"errors={'; '.join(errors[:3])}"
        )
    else:
        return
    try:
        enqueue_message(text=text, chat_id=admin_chat_id, task_id=task_id, message_kind="alert")
    except Exception:
        pass


def run_routed_task(task: dict[str, Any], *, config: dict[str, Any] | None = None, output_path: Path | None = None, forced_provider: str = "") -> dict[str, Any]:
    conf = config or load_router_config()
    registry = load_registry()
    decision = build_route_decision(task, config=conf)
    normalized_task = decision["task"]
    chain = list(decision["candidate_chain"])
    if forced_provider:
        chain = [item for item in chain if str(item.get("provider", "")) == forced_provider]
        if not chain:
            # forced_provider not in candidate_chain (e.g. manual-only provider)
            # Build a synthetic candidate from provider_policies if it exists
            fp_policy = conf.get("provider_policies", {}).get(forced_provider)
            if fp_policy:
                chain = [{"provider": forced_provider, "timeout_sec": fp_policy.get("timeout_sec", 120.0), "retry_count": fp_policy.get("retry_count", 0), "model_name": ""}]
    if not normalized_task["allow_fallback"] and chain:
        chain = chain[:1]
    attempts: list[dict[str, Any]] = []
    result: dict[str, Any] = {}
    ok = False
    selected_provider = ""
    fallback_from = ""
    base_output = output_path or REPORTS_DIR / f"{normalized_task['task_id']}.json"
    log_path = REPORTS_DIR / f"{normalized_task['task_id']}.exec.log"
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    for index, candidate in enumerate(chain):
        provider = str(candidate["provider"])
        retries = max(0, int(candidate.get("retry_count", 0) or 0))
        timeout_sec = float(candidate.get("timeout_sec", conf.get("default_timeout_sec", 120.0)) or 120.0)
        model_name = str(candidate.get("model_name", "")).strip()
        for retry in range(retries + 1):
            success, payload = invoke_provider(
                provider=provider,
                task=normalized_task,
                timeout_sec=timeout_sec,
                log_path=log_path,
                registry=registry,
                model_override=model_name,
            )
            attempt_record = {
                "provider": provider,
                "model_name": model_name,
                "retry_index": retry,
                "timeout_sec": timeout_sec,
                "ok": success,
                **payload,
            }
            attempts.append(attempt_record)
            if success:
                ok = True
                selected_provider = provider
                if index > 0:
                    fallback_from = str(chain[0]["provider"])
                result = payload
                break
        if ok:
            break

    envelope = {
        "ok": ok,
        "task": normalized_task,
        "classification": decision["classification"],
        "route_decision": {
            "selected_provider": selected_provider or decision.get("selected_provider", ""),
            "fallback_from": fallback_from,
            "candidate_chain": chain,
            "filtered_providers": decision["filtered_providers"],
            "operational_notes": decision.get("operational_notes", []),
            "generated_at": decision["generated_at"],
        },
        "attempts": attempts,
        "result": result,
        "output_path": str(base_output),
        "recorded_at": utc_now_iso(),
    }
    _atomic_write_json(base_output, envelope)

    # ── Provider cost tracking: log token usage for all API attempts ──
    for attempt in attempts:
        usage = attempt.get("usage")
        if isinstance(usage, dict) and usage:
            _append_usage_ledger(
                provider=str(attempt.get("provider", "")),
                model_name=str(attempt.get("model_name", "")),
                task_id=normalized_task["task_id"],
                usage=usage,
            )
        elif not attempt.get("ok") and attempt.get("error"):
            _append_usage_ledger(
                provider=str(attempt.get("provider", "")),
                model_name=str(attempt.get("model_name", "")),
                task_id=normalized_task["task_id"],
                usage={"error": str(attempt["error"])[:200], "input_tokens": 0, "output_tokens": 0},
            )

    # ── Graceful degradation: alert admin when provider fallback occurs ──
    if fallback_from:
        _emit_fallback_alert(envelope)
    elif not ok:
        _emit_fallback_alert(envelope)

    return envelope


def _load_task_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.from_json_file and args.from_stdin:
        raise ValueError("use only one of --from-json-file or --from-stdin")
    if args.from_json_file:
        payload = json.loads(Path(args.from_json_file).read_text(encoding="utf-8"))
    elif args.from_stdin:
        payload = json.load(sys.stdin)
    else:
        raise ValueError("task input required")
    if not isinstance(payload, dict):
        raise ValueError("task input must be a JSON object")
    return payload


def cmd_route(args: argparse.Namespace) -> int:
    payload = _load_task_payload(args)
    decision = build_route_decision(payload)
    print(json.dumps(decision, ensure_ascii=True, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    payload = _load_task_payload(args)
    output = Path(args.output) if args.output else None
    envelope = run_routed_task(payload, output_path=output, forced_provider=str(args.force_provider or "").strip().lower())
    print(json.dumps(envelope, ensure_ascii=True, indent=2))
    return 0 if envelope["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Route bounded model work across local and remote providers")
    parser.add_argument(
        "--internal-handler-cli",
        action="store_true",
        help="Acknowledge that scripts/model_router.py is an internal handler CLI, not the preferred active AGN surface.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    route_parser = sub.add_parser("route", help="Classify a task and show the routing decision")
    route_parser.add_argument("--from-json-file", help="Read task payload JSON from file")
    route_parser.add_argument("--from-stdin", action="store_true", help="Read task payload JSON from stdin")
    route_parser.set_defaults(func=cmd_route)

    run_parser = sub.add_parser("run", help="Route and execute a task")
    run_parser.add_argument("--from-json-file", help="Read task payload JSON from file")
    run_parser.add_argument("--from-stdin", action="store_true", help="Read task payload JSON from stdin")
    run_parser.add_argument("--output", help="Write envelope JSON to this path")
    run_parser.add_argument("--force-provider", choices=["qwen_local", "deepseek", "gemini", "claude", "vertex_local"], default="")
    run_parser.set_defaults(func=cmd_run)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if should_block_direct_handler_cli(bool(getattr(args, "internal_handler_cli", False))):
        print(
            render_direct_handler_cli_block(
                handler_id="model_router",
                purpose="Bounded provider routing and execution handler behind governed AGN surfaces.",
                recommended_entrypoints=[
                    "python3 scripts/agent_collaboration.py route --from-json-file <task.json>",
                    "python3 scripts/agent_collaboration.py run --from-json-file <task.json> --output <out.json>",
                    "python3 scripts/agn_governed_execution.py provider --from-json-file <task.json>",
                ],
                notes=[
                    "Use the explicit override flag only for validation, compatibility, or implementation-level inspection.",
                    "Active AGN work should prefer governed facades instead of direct handler CLIs.",
                ],
            )
        )
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
