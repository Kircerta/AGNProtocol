from __future__ import annotations

from typing import Any


VALID_STATUSES = {"pending", "needs_review", "approved", "rejected", "halted", "awaiting_utility"}
DAILY_RESEARCH_REQUIRED_FIELDS = {
    "task_kind",
    "research_trigger_mode",
    "research_axis",
    "question",
    "hypothesis",
    "baseline",
    "single_change",
    "budget",
    "round",
    "proposal_version",
    "decision_mode",
    "failure_mode_allowed",
}

# Documented legal state transitions.  Not enforced at runtime yet, but
# available for validation scripts and tests.
VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"needs_review", "rejected", "halted"},
    "needs_review": {"approved", "rejected", "halted"},
    "rejected": {"pending", "needs_review", "halted"},
    "halted": {"pending", "needs_review"},  # only via admin unlock
    "awaiting_utility": {"pending", "halted"},
    "approved": set(),  # terminal state
}


def derive_status(task: dict[str, Any]) -> str:
    # Halted takes priority over other states.
    if str(task.get("lock_state", "")).strip().lower() == "halted":
        return "halted"

    # Awaiting utility command approval.
    if task.get("awaiting_utility") is True:
        return "awaiting_utility"

    decision = task.get("decision")
    if decision == "approved":
        return "approved"
    if decision == "rejected":
        return "rejected"

    if task.get("review_requested") is True:
        return "needs_review"

    return "pending"


def validate_transition(from_status: str, to_status: str) -> bool:
    """Return True if the transition is legal per VALID_TRANSITIONS."""
    allowed = VALID_TRANSITIONS.get(from_status)
    if allowed is None:
        return False
    return to_status in allowed


def validate_daily_research_contract(task: dict[str, Any]) -> list[str]:
    if str(task.get("task_kind", "")).strip() != "daily_research":
        return []

    errors: list[str] = []
    missing = sorted(field for field in DAILY_RESEARCH_REQUIRED_FIELDS if field not in task)
    if missing:
        errors.append(f"missing_daily_research_fields:{','.join(missing)}")

    for key in ("research_axis", "question", "hypothesis", "baseline", "single_change", "decision_mode"):
        value = task.get(key)
        if not isinstance(value, str):
            errors.append(f"{key}_must_be_string")
            continue
        if key != "research_axis" and not value.strip():
            errors.append(f"{key}_must_be_non_empty")

    budget = task.get("budget")
    if not isinstance(budget, dict) or not budget:
        errors.append("budget_must_be_object")

    for key in ("round", "proposal_version"):
        value = task.get(key)
        if not isinstance(value, int) or value < 0:
            errors.append(f"{key}_must_be_non_negative_int")

    trigger_mode = str(task.get("research_trigger_mode", "")).strip().lower()
    if trigger_mode not in {"manual", "auto"}:
        errors.append("research_trigger_mode_invalid")

    if task.get("failure_mode_allowed") is not True:
        errors.append("failure_mode_allowed_must_be_true")

    return errors
