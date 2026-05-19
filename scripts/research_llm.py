#!/usr/bin/env python3
"""LLM-powered research primitives for AGN research pipeline.

Provides real AI-driven research capabilities:
- Web survey of recent AI topics via Brave Search API
- LLM-powered experiment code generation
- Sandboxed Python experiment execution
- LLM-powered research essay writing

All functions are designed as drop-in upgrades for the existing stub
implementations in research_worker.py and research_flow.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

BRAVE_API_KEY = os.getenv("BRAVE_SEARCH_API_KEY", "").strip()
BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
EXPERIMENT_TIMEOUT_SEC = 120
EXPERIMENT_MAX_OUTPUT_BYTES = 64 * 1024


# ── Brave Web Search ─────────────────────────────────────────────


def _load_brave_key() -> str:
    """Load Brave API key from env or openclaw config."""
    key = BRAVE_API_KEY
    if key:
        return key
    try:
        config_path = Path.home() / ".openclaw" / "openclaw.json"
        if config_path.exists():
            config = json.loads(config_path.read_text(encoding="utf-8"))
            key = str(config.get("tools", {}).get("web", {}).get("search", {}).get("apiKey", "")).strip()
    except Exception:
        pass
    return key


def survey_ai_topics(query: str = "latest AI machine learning research breakthroughs", limit: int = 10) -> list[dict[str, str]]:
    """Search Brave for recent AI research topics. Returns list of {title, url, description}."""
    api_key = _load_brave_key()
    if not api_key:
        return []
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                BRAVE_SEARCH_URL,
                params={"q": query, "count": min(limit, 20), "freshness": "pw"},
                headers={"Accept": "application/json", "Accept-Encoding": "gzip", "X-Subscription-Token": api_key},
            )
            resp.raise_for_status()
            data = resp.json()
        results = []
        for item in data.get("web", {}).get("results", [])[:limit]:
            results.append({
                "title": str(item.get("title", "")).strip(),
                "url": str(item.get("url", "")).strip(),
                "description": str(item.get("description", "")).strip()[:500],
            })
        return results
    except Exception:
        return []


# ── LLM Invocation ───────────────────────────────────────────────


def _invoke_llm(prompt: str, system: str = "", response_mode: str = "text") -> str:
    """Invoke the governed provider path to generate text."""
    try:
        from agn_governed_execution import dispatch_provider_task
        task_payload = {
            "task_id": f"research-llm-{int(time.time())}",
            "instruction": prompt,
            "system_prompt": system or "You are a research assistant. Be precise and concise.",
            "response_mode": response_mode,
            "allow_fallback": True,
            "complexity": "medium",
            "risk": "low",
        }
        routed = dispatch_provider_task(
            task_payload,
            caller="research_llm",
            task_id=str(task_payload["task_id"]),
            trace_id=f"trace-{task_payload['task_id']}",
            intent="research_llm_generation",
            reason="research pipeline governed provider execution",
            risk_level="low",
        )
        envelope = routed.get("envelope", {}) if isinstance(routed.get("envelope", {}), dict) else {}
        if routed.get("ok") and envelope.get("ok"):
            return str(envelope.get("result", {}).get("content", "")).strip()
    except Exception:
        pass
    return ""


def _invoke_llm_json(prompt: str, system: str = "") -> dict[str, Any]:
    """Invoke LLM and parse JSON response."""
    raw = _invoke_llm(prompt, system=system, response_mode="json_object")
    if not raw:
        return {}
    # Extract JSON from response (may be wrapped in markdown).
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        start = 1
        end = len(lines)
        for i, line in enumerate(lines[1:], 1):
            if line.strip().startswith("```"):
                end = i
                break
        text = "\n".join(lines[start:end])
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


# ── Experiment Code Generation & Execution ──────────────────────


def generate_experiment_code(proposal: dict[str, Any], strategy: str = "full") -> str:
    """Use LLM to generate a Python experiment script from the research proposal."""
    problem = str(proposal.get("problem", "")).strip()
    core_idea = str(proposal.get("core_idea", "")).strip()
    baseline = str(proposal.get("baseline", "")).strip()
    single_change = str(proposal.get("single_change", "")).strip()
    title = str(proposal.get("title", "")).strip()

    prompt = textwrap.dedent(f"""\
        Write a self-contained Python experiment script that tests this hypothesis.

        ## Research Question
        {problem}

        ## Core Idea
        {core_idea}

        ## Baseline Method
        {baseline}

        ## Proposed Change (Single Variable)
        {single_change}

        ## Requirements
        - The script must be completely self-contained (no external data files)
        - Use only Python stdlib + numpy (if needed, generate synthetic data)
        - Strategy: {"full comparison baseline vs proposed" if strategy == "full" else "baseline only"}
        - Print results as a JSON object on the LAST line of stdout
        - The JSON must contain: {{"baseline_accuracy": float, "proposed_accuracy": float, "improvement": float, "case_count": int, "method": str, "notes": [str]}}
        - Keep the experiment small: max 1000 data points, max 60 seconds runtime
        - Do NOT use matplotlib or any display libraries
        - Do NOT import torch, tensorflow, or heavy ML frameworks
        - Use numpy or pure Python for all computation

        Write ONLY the Python code, no explanations. Start with imports.
    """)

    code = _invoke_llm(prompt, system="You are a Python experiment code generator. Output ONLY valid Python code, no markdown fences, no explanations.")
    # Strip markdown fences if present.
    if code.startswith("```"):
        lines = code.splitlines()
        start = 1
        end = len(lines)
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "```":
                end = i
                break
        code = "\n".join(lines[start:end])
    return code.strip()


def execute_experiment_code(code: str, timeout_sec: float = EXPERIMENT_TIMEOUT_SEC) -> dict[str, Any]:
    """Execute generated Python code in a sandboxed subprocess and parse JSON results."""
    if not code.strip():
        return {"error": "empty_code", "status": "failure_note"}

    # Write code to temp file.
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(code)
        script_path = f.name

    try:
        started = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, script_path],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            cwd=tempfile.gettempdir(),
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        duration_sec = round(time.perf_counter() - started, 2)

        stdout = proc.stdout[-EXPERIMENT_MAX_OUTPUT_BYTES:] if proc.stdout else ""
        stderr = proc.stderr[-4096:] if proc.stderr else ""

        if proc.returncode != 0:
            return {
                "error": f"experiment_exit_code_{proc.returncode}",
                "stderr": stderr[:1000],
                "status": "failure_note",
                "duration_sec": duration_sec,
            }

        # Parse the last line as JSON result.
        lines = [l.strip() for l in stdout.strip().splitlines() if l.strip()]
        if not lines:
            return {"error": "no_output", "status": "failure_note", "duration_sec": duration_sec}

        # Try to find JSON in output (last line first, then full output).
        result = None
        for line in reversed(lines):
            try:
                result = json.loads(line)
                if isinstance(result, dict):
                    break
                result = None
            except json.JSONDecodeError:
                continue

        if not isinstance(result, dict):
            return {
                "error": "no_json_in_output",
                "stdout_tail": stdout[-500:],
                "status": "failure_note",
                "duration_sec": duration_sec,
            }

        result["duration_sec"] = duration_sec
        result["status"] = "ok"
        return result

    except subprocess.TimeoutExpired:
        return {"error": f"experiment_timeout_{timeout_sec}s", "status": "failure_note"}
    except Exception as exc:
        return {"error": f"experiment_exception:{type(exc).__name__}", "status": "failure_note"}
    finally:
        try:
            os.unlink(script_path)
        except Exception:
            pass


def run_llm_experiment(proposal: dict[str, Any], strategy: str = "full") -> dict[str, Any]:
    """Full LLM-powered experiment: generate code, execute, return results.

    Returns a dict compatible with the research_worker result schema.
    """
    topic_id = str(proposal.get("topic_id", "")).strip()
    title = str(proposal.get("title", "")).strip()

    # Generate experiment code.
    code = generate_experiment_code(proposal, strategy=strategy)
    if not code:
        return {
            "topic_id": topic_id,
            "title": title,
            "strategy": strategy,
            "status": "failure_note",
            "empirical_execution": False,
            "truthfulness_status": "failure_note",
            "truthfulness_reason": "llm_code_generation_failed",
            "metrics": {},
            "notes": ["LLM failed to generate experiment code."],
            "error": "code_generation_failed",
            "experiment_code": "",
        }

    # Execute the generated code.
    exec_result = execute_experiment_code(code)

    if exec_result.get("status") != "ok":
        return {
            "topic_id": topic_id,
            "title": title,
            "strategy": strategy,
            "status": "failure_note",
            "empirical_execution": True,
            "truthfulness_status": "empirical",
            "truthfulness_reason": "code_executed_but_failed",
            "metrics": {},
            "notes": [
                f"Generated Python experiment executed but failed: {exec_result.get('error', 'unknown')}",
                f"Duration: {exec_result.get('duration_sec', '?')}s",
            ],
            "error": exec_result.get("error", "execution_failed"),
            "experiment_code": code[:4000],
        }

    # Build metrics from execution result.
    metrics = {
        "baseline_accuracy": exec_result.get("baseline_accuracy", 0),
        "proposed_accuracy": exec_result.get("proposed_accuracy", 0),
        "improvement": exec_result.get("improvement", 0),
        "case_count": exec_result.get("case_count", 0),
        "duration_sec": exec_result.get("duration_sec", 0),
    }
    # Include any extra metrics the experiment reported.
    for k, v in exec_result.items():
        if k not in {"status", "error", "duration_sec"} and k not in metrics:
            if isinstance(v, (int, float, str, bool)):
                metrics[k] = v

    notes = exec_result.get("notes", [])
    if not isinstance(notes, list):
        notes = [str(notes)] if notes else []
    method = exec_result.get("method", "")
    if method:
        notes.insert(0, f"Method: {method}")
    notes.append(f"Experiment generated by LLM and executed locally in {exec_result.get('duration_sec', '?')}s")

    return {
        "topic_id": topic_id,
        "title": title,
        "strategy": strategy,
        "status": "ok",
        "empirical_execution": True,
        "truthfulness_status": "empirical",
        "truthfulness_reason": "llm_generated_code_executed_locally",
        "metrics": metrics,
        "notes": notes[:8],
        "experiment_code": code[:4000],
    }


# ── LLM-Powered Essay Writing ───────────────────────────────────


def write_research_essay(
    *,
    proposal: dict[str, Any],
    result: dict[str, Any],
    task: dict[str, Any],
) -> str:
    """Use LLM to write a proper research essay from experiment results."""
    problem = str(proposal.get("problem", "")).strip()
    core_idea = str(proposal.get("core_idea", "")).strip()
    baseline = str(proposal.get("baseline", "")).strip()
    single_change = str(proposal.get("single_change", "")).strip()
    title = str(proposal.get("title", "")).strip()
    question = str(task.get("question", "")).strip()
    hypothesis = str(task.get("hypothesis", "")).strip()
    metrics = result.get("metrics", {})
    notes = result.get("notes", [])
    experiment_code = str(result.get("experiment_code", "")).strip()

    metrics_text = "\n".join(f"- {k}: {v}" for k, v in metrics.items() if k != "duration_sec")
    notes_text = "\n".join(f"- {n}" for n in notes[:5]) if notes else "No additional notes."

    prompt = textwrap.dedent(f"""\
        Write a concise research mini-paper (800-1200 words) based on the following experiment.

        ## Title
        {title}

        ## Research Question
        {question or problem}

        ## Hypothesis
        {hypothesis or core_idea}

        ## Baseline
        {baseline}

        ## Proposed Change
        {single_change}

        ## Experiment Results
        {metrics_text}

        ## Execution Notes
        {notes_text}

        ## Format Requirements
        Write in markdown with these sections:
        1. # {title}
        2. ## Abstract (2-3 sentences)
        3. ## Introduction (why this matters)
        4. ## Method (what was tested and how)
        5. ## Results (present the metrics clearly)
        6. ## Discussion (interpret results, limitations)
        7. ## Conclusion (1-2 sentences)

        Be precise, factual, and only claim what the metrics support.
        Do NOT fabricate additional metrics or claim results not in the data.
    """)

    essay = _invoke_llm(prompt, system="You are a scientific paper writer. Write precise, factual research papers. Only state claims supported by the provided experimental evidence.")
    if not essay.strip():
        # Fallback to template if LLM fails.
        return _template_essay(proposal=proposal, result=result, task=task)
    return essay.strip()


def _template_essay(*, proposal: dict[str, Any], result: dict[str, Any], task: dict[str, Any]) -> str:
    """Fallback template-based essay when LLM is unavailable."""
    title = str(proposal.get("title", "")).strip() or "Research Unit"
    problem = str(proposal.get("problem", "")).strip()
    core_idea = str(proposal.get("core_idea", "")).strip()
    metrics = result.get("metrics", {})
    notes = result.get("notes", [])

    metrics_lines = [f"- **{k}**: {v}" for k, v in metrics.items() if k != "duration_sec"]
    notes_lines = [f"- {n}" for n in notes[:5]]

    return "\n".join([
        f"# {title}",
        "",
        "## Abstract",
        f"This study investigates: {problem}",
        f"Core approach: {core_idea}",
        "",
        "## Results",
        *metrics_lines,
        "",
        "## Notes",
        *notes_lines,
        "",
        "## Conclusion",
        "Results are bounded to the synthetic experiment scope described above.",
    ])


# ── Topic Selection via LLM ─────────────────────────────────────


def select_research_topic(web_results: list[dict[str, str]], profile: dict[str, Any]) -> dict[str, Any]:
    """Use LLM to select the best research topic from web search results."""
    if not web_results:
        return {}

    focus = profile.get("focus_topics", [])
    axes = profile.get("allowed_axes", [])

    results_text = "\n".join(
        f"{i+1}. {r['title']}\n   {r['description']}\n   URL: {r['url']}"
        for i, r in enumerate(web_results[:10])
    )

    prompt = textwrap.dedent(f"""\
        From the following recent AI research results, select the ONE most promising
        topic for a small-scale reproducible experiment.

        ## Focus Areas (preferred)
        {', '.join(str(t) for t in focus[:5]) if focus else 'Any AI/ML topic'}

        ## Research Axes
        {', '.join(str(a) for a in axes[:5]) if axes else 'Any'}

        ## Recent Results
        {results_text}

        ## Selection Criteria
        - Must be testable with a small Python experiment (no GPU required)
        - Must have a clear baseline vs proposed comparison
        - Prefer topics with practical impact
        - Avoid topics requiring large datasets or special hardware

        Respond with a JSON object:
        {{
            "topic_id": "auto-YYYY-MM-DD-short-slug",
            "title": "concise title",
            "problem": "what problem does this address",
            "core_idea": "the key insight or method",
            "baseline": "what to compare against",
            "single_change": "the one thing being changed",
            "research_axis": "which axis this falls under",
            "source_url": "URL of the source",
            "method_family": "experiment_type",
            "required_method_family": ""
        }}
    """)

    result = _invoke_llm_json(prompt, system="You are a research topic selector. Return valid JSON only.")
    if result and result.get("topic_id"):
        # Ensure topic_id has date prefix.
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        tid = str(result.get("topic_id", "")).strip()
        if not tid.startswith("auto-"):
            tid = f"auto-{today}-{tid}"
        result["topic_id"] = tid
    return result
