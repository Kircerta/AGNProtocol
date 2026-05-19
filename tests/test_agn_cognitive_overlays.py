from __future__ import annotations

from scripts.agn_cognitive_overlays import recommend_overlays


def test_recommend_overlays_for_coding_task() -> None:
    payload = recommend_overlays("Implement a safer task dispatcher and add tests")
    names = {item["name"] for item in payload}
    assert "coding-criticality" in names


def test_recommend_overlays_for_academic_writing_task() -> None:
    payload = recommend_overlays("Write a literature review and improve the abstract")
    names = {item["name"] for item in payload}
    assert "academic-writing-critic" in names
