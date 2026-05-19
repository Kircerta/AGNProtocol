from __future__ import annotations

from typing import Any, Literal, TypedDict


ControlType = Literal["PAUSE", "RESUME", "STOP", "STATUS", "MODIFY"]


class AGNControlPayload(TypedDict, total=False):
    request_text: str
    request_summary: str
    request_text_ref: str
    acceptance_criteria: list[dict[str, Any]]
    needs_context_read: bool
    context_read_path: str


class AGNControlCreateRequest(TypedDict, total=False):
    control_type: ControlType
    control_id: str
    payload: AGNControlPayload


class OverviewResponse(TypedDict):
    task_counts_by_state: dict[str, int]
    queue_counts: dict[str, int]
    watchdog_summary: dict[str, Any]
    last_tick_utc: str
