#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import hashlib
import json
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
VERDICTS_DIR = ROOT / "verdicts"



def canonical_issues(issues: Any) -> str:
    return json.dumps(issues, ensure_ascii=True, sort_keys=True, separators=(",", ":"))



def verdict_path(task_id: str, attempt: int) -> Path:
    return VERDICTS_DIR / f"{task_id}.{attempt}.json"



def load_issues(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    issues = data.get("issues")
    if not isinstance(issues, list):
        raise ValueError(f"issues must be list in {path}")
    return issues



def run(task_id: str, base_attempt: int, candidate_attempt: int) -> int:
    base_path = verdict_path(task_id, base_attempt)
    candidate_path = verdict_path(task_id, candidate_attempt)

    if not base_path.exists() or not candidate_path.exists():
        print("fidelity_ok=false reason=missing_verdict")
        return 1

    base_issues = load_issues(base_path)
    candidate_issues = load_issues(candidate_path)

    base_canonical = canonical_issues(base_issues)
    candidate_canonical = canonical_issues(candidate_issues)

    if base_canonical != candidate_canonical:
        base_hash = hashlib.sha256(base_canonical.encode("utf-8")).hexdigest()
        candidate_hash = hashlib.sha256(candidate_canonical.encode("utf-8")).hexdigest()
        print(f"fidelity_ok=false base_hash={base_hash} candidate_hash={candidate_hash}")
        return 1

    canonical_hash = hashlib.sha256(base_canonical.encode("utf-8")).hexdigest()
    print(f"fidelity_ok=true issues_sha256={canonical_hash}")
    return 0



def main() -> int:
    parser = argparse.ArgumentParser(description="Fidelity audit for redispatch issues consistency")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--base-attempt", type=int, required=True)
    parser.add_argument("--candidate-attempt", type=int, required=True)
    args = parser.parse_args()

    try:
        return run(
            task_id=args.task_id,
            base_attempt=args.base_attempt,
            candidate_attempt=args.candidate_attempt,
        )
    except Exception as exc:
        print(f"fidelity_ok=false reason={type(exc).__name__}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
