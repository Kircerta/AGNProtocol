from __future__ import annotations

import json
from pathlib import Path

from scripts.safety import high_risk_guardrails as guardrails


def test_plan_delete_blocks_repo_root(tmp_path: Path) -> None:
    output = tmp_path / "delete_plan.json"
    rc = guardrails.plan_delete(root=guardrails.ROOT, pattern="*.py", output=output)
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert rc == 1
    assert payload["ok"] is False
    assert payload["guardrail_status"] == "blocked"


def test_plan_rename_blocks_colliding_targets(tmp_path: Path) -> None:
    src_a = tmp_path / "a.txt"
    src_b = tmp_path / "b.txt"
    dst = tmp_path / "renamed.txt"
    src_a.write_text("a", encoding="utf-8")
    src_b.write_text("b", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "operations": [
                    {"from": str(src_a), "to": str(dst)},
                    {"from": str(src_b), "to": str(dst)},
                ]
            },
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )
    output = tmp_path / "rename_plan.json"
    rc = guardrails.plan_rename(manifest_path=manifest, output=output)
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert rc == 1
    assert payload["ok"] is False
    assert any("target_collision" in item for item in payload["errors"])


def test_plan_publish_requires_approval(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    output = tmp_path / "publish_plan.json"
    rc = guardrails.plan_publish(
        repo_path=repo,
        remote="origin",
        branch="main",
        files=["README.md"],
        allow_external_publish=False,
        admin_approved=False,
        output=output,
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert rc == 1
    assert payload["ok"] is False
    assert "allow_external_publish_not_set" in payload["errors"]
    assert "admin_approved_not_set" in payload["errors"]
