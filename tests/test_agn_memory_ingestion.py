from __future__ import annotations

import json

from scripts.agn_memory_ingestion import build_manual_records, build_refresh_records


def test_memory_ingestion_builds_manual_records_with_constraint_and_todo() -> None:
    records = build_manual_records(
        change_kind="skill",
        name="agn-control-plane-operator-posture",
        summary="Codex should decide when to switch to Control Plane or formal command path.",
        operating_impact="Strengthens governance-first posture.",
        source_refs=["/tmp/skill.md"],
        scope="agn2/codex",
        related_task="task-1",
        author="codex",
        confidence="high",
        constraints=["Do not mutate runtime internals directly."],
        follow_ups=["Teach the new posture through local skill instructions."],
    )
    assert records[0]["kind"] == "decision"
    assert records[0]["fact_payload"]["do_not_compress"] is True
    assert any(record["kind"] == "constraint" for record in records)
    assert any(record["kind"] == "todo" for record in records)


def test_memory_ingestion_builds_records_from_refresh_report(tmp_path) -> None:
    report = tmp_path / "refresh.json"
    report.write_text(
        json.dumps(
            {
                "diff": {
                    "changed_count": 2,
                    "changed_groups": ["agn_skills", "repo_capability_surfaces"],
                    "added": ["/tmp/a"],
                    "removed": [],
                    "modified": ["/tmp/b"],
                },
                "recommended_actions": ["Append a structured memory record."],
            }
        ),
        encoding="utf-8",
    )
    records = build_refresh_records(
        report_path=str(report),
        scope="agn2/codex",
        author="codex",
        confidence="high",
        related_task="task-2",
    )
    assert records[0]["kind"] == "status"
    assert "changed files across" in records[0]["summary"]
    assert any(record["kind"] == "todo" for record in records)
